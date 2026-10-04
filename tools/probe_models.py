"""模型探针：在真实 GL 上下文里加载模型，量出取景所需的映射关系。

为什么需要：手机端与桌面端现在要**同时保留**两个模型并支持切换，
而桌面端的取景（scale / offset）以前是照着旧模型硬编码的。这个脚本把
「画布多大 / 各档 scale 下美术落在哪 / offset 的单位是多少像素」一次性量出来。

结论会写进 backend/models_catalog.py，并驱动 ui/live2d_view.py 的自动取景。

用法：
    .venv\\Scripts\\python.exe tools\\probe_models.py                # 两个模型，默认档位
    .venv\\Scripts\\python.exe tools\\probe_models.py v5 0.02 0.05 0.1 0.5 1.0
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

MODELS = {
    "v4": BASE_DIR / "assets" / "live2d" / "miku" / "miku.model3.json",
    "v5": BASE_DIR / "models" / "miku_v5" / "miku.model3.json",
}

import OpenGL.GL as gl  # noqa: E402
import live2d.v3 as live2d  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtOpenGLWidgets import QOpenGLWidget  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

WIN_W, WIN_H = 360, 660
OUT_DIR = BASE_DIR / ".tmp" / "probe"
DEFAULT_SCALES = [0.02, 0.05, 0.1, 0.25, 0.5, 1.0]
STEP = 14          # 每档之间隔多少帧（够动画稳定 + 测完）
SETTLE = 9         # 设定之后等几帧再量


class Probe(QOpenGLWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.resize(WIN_W, WIN_H)
        self.scale_factor = QGuiApplication.primaryScreen().devicePixelRatio()

        self.key = ""
        self.path = Path()
        self.scales: list[float] = list(DEFAULT_SCALES)
        self.model: live2d.LAppModel | None = None
        self.frames = 0
        self.report: dict = {}
        self.boxes: dict = {}
        self.plan: list[tuple[int, str, float, tuple[float, float]]] = []
        self.index = 0

    # ------------------------------------------------------------- OpenGL
    def initializeGL(self) -> None:
        live2d.glInit()
        print(f"[GL] renderer={gl.glGetString(gl.GL_RENDERER)}")
        self._load_current()
        self.startTimer(16)

    def resizeGL(self, w: int, h: int) -> None:  # noqa: N802
        if self.model is not None:
            self.model.Resize(w, h)

    def paintGL(self) -> None:  # noqa: N802
        live2d.clearBuffer(0.0, 0.0, 0.0, 0.0)
        if self.model is None:
            return
        self.model.Update()
        self.model.Draw()

    def _load_current(self) -> None:
        self.model = live2d.LAppModel()
        self.model.LoadModelJson(str(self.path))
        self.model.SetAutoBlinkEnable(True)
        self.model.SetAutoBreathEnable(True)

        facts = {
            "model3": str(self.path.relative_to(BASE_DIR)),
            "canvas_unit": list(self.model.GetCanvasSize()),
            "canvas_pixel": list(self.model.GetCanvasSizePixel()),
            "pixels_per_unit": self.model.GetPixelsPerUnit(),
            # 注意：C++ 侧**没有** GetPartCount（live2d-py 那个名字会抛 AttributeError）
            "parts": len(self.model.GetPartIds()),
            "params": self.model.GetParameterCount(),
            "drawables": len(self.model.GetDrawableIds()),
        }

        # model3.json 里没写 Expressions / Motions 时，从同目录的 exp3 / motion3 补进来。
        # 这是桌面端支持新模型的关键：Cubism 运行时不会自动发现这些散装文件。
        exps = list(self.model.GetExpressionIds())
        if not exps:
            for f in sorted(self.path.parent.glob("*.exp3.json")):
                self.model.LoadExtraExpression(f.name[: -len(".exp3.json")], str(f))
            exps = list(self.model.GetExpressionIds())
        facts["expressions"] = exps

        groups = dict(self.model.GetMotionGroups())
        if not groups:
            for f in sorted(self.path.parent.glob("*.motion3.json")):
                self.model.LoadExtraMotion("Idle", str(f))
            groups = dict(self.model.GetMotionGroups())
        facts["motion_groups"] = groups

        ids = sorted(self.model.GetParamIds())
        facts["has_Param137"] = "Param137" in ids
        facts["has_ParamMouthOpenY"] = "ParamMouthOpenY" in ids
        facts["has_ParamBreath"] = "ParamBreath" in ids
        facts["has_PARAM_BREATH"] = "PARAM_BREATH" in ids
        facts["has_ParamAngleZ"] = "ParamAngleZ" in ids
        self.report = facts
        print(f"\n=== FACTS {self.key} ===")
        print(json.dumps(facts, ensure_ascii=False, indent=2))

        # 档位计划：每档「设定 → 等 SETTLE 帧 → 量包围盒 + 截图」
        # 最后一档专门用来反推 offset 的单位（像素 / 0.1 offset）。
        self.plan = []
        frame = 20
        for s in self.scales:
            self.plan.append((frame, f"s{s:g}", float(s), (0.0, 0.0)))
            frame += STEP
        mid = self.scales[len(self.scales) // 2]
        self.plan.append((frame, "dx", float(mid), (0.1, 0.0)))
        frame += STEP
        self.plan.append((frame, "dy", float(mid), (0.0, 0.1)))
        self.index = 0

    # -------------------------------------------------------------- 帧循环
    def timerEvent(self, event) -> None:  # noqa: N802
        if self.model is None or self.index >= len(self.plan):
            return
        self.frames += 1
        start, label, scale, offset = self.plan[self.index]

        if self.frames == start:
            self.model.SetScale(scale)
            self.model.SetOffset(*offset)
            return
        if self.frames >= start + SETTLE:
            box = self._alpha_box()
            self.boxes[label] = box
            out = OUT_DIR / f"{self.key}_{label}.png"
            self.grabFramebuffer().save(str(out))
            print(f"  [{self.key}/{label}] scale={scale} offset={offset} bbox={box}")
            self.index += 1
            return
        self.update()

    def _alpha_box(self):
        """读整块 framebuffer 的 alpha，返回美术包围盒（逻辑像素，原点左上）。"""
        w = int(self.width() * self.scale_factor)
        h = int(self.height() * self.scale_factor)
        self.makeCurrent()
        try:
            raw = gl.glReadPixels(0, 0, w, h, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
        finally:
            self.doneCurrent()
        if not raw:
            return None
        arr = np.frombuffer(raw, dtype=np.uint8)
        if arr.size < w * h * 4:
            return None
        alpha = arr.reshape(h, w, 4)[:, :, 3]
        cols = np.where(alpha.max(axis=0) > 8)[0]
        rows = np.where(alpha.max(axis=1) > 8)[0]
        if len(rows) == 0 or len(cols) == 0:
            return None
        # GL 原点在左下：行号越大越靠上，所以要翻转
        top = (h - 1 - int(rows.max())) / self.scale_factor
        bottom = (h - 1 - int(rows.min())) / self.scale_factor
        left = int(cols.min()) / self.scale_factor
        right = int(cols.max()) / self.scale_factor
        return [round(left, 1), round(top, 1), round(right, 1), round(bottom, 1)]

    # -------------------------------------------------------------- 收尾
    def finish(self) -> None:
        self.report["boxes"] = self.boxes
        # scale → 美术屏幕尺寸，验证线性关系
        sweep = []
        for s in self.scales:
            b = self.boxes.get(f"s{s:g}")
            if not b:
                continue
            sweep.append({
                "scale": s,
                "w": round(b[2] - b[0], 1),
                "h": round(b[3] - b[1], 1),
                "cx": round((b[0] + b[2]) / 2, 1),
                "cy": round((b[1] + b[3]) / 2, 1),
                "w_per_scale": round((b[2] - b[0]) / s, 2),
                "h_per_scale": round((b[3] - b[1]) / s, 2),
            })
        derived = {"sweep": sweep}
        mid = self.scales[len(self.scales) // 2]
        b0 = self.boxes.get(f"s{mid:g}")
        bx = self.boxes.get("dx")
        by = self.boxes.get("dy")
        if b0 and bx:
            derived["px_per_offset_x"] = round(
                ((bx[0] + bx[2]) / 2 - (b0[0] + b0[2]) / 2) / 0.1, 2
            )
        if b0 and by:
            derived["px_per_offset_y"] = round(
                ((by[1] + by[3]) / 2 - (b0[1] + b0[3]) / 2) / 0.1, 2
            )
        self.report["derived"] = derived
        print(f"\n=== DERIVED {self.key} ===")
        print(json.dumps(derived, ensure_ascii=False, indent=2))
        out = OUT_DIR / f"report_{self.key}.json"
        out.write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")


def run_one(key: str, scales: list[float]) -> dict:
    app = QApplication.instance()
    win = Probe()
    win.key = key
    win.path = MODELS[key]
    win.scales = scales
    win.show()
    win.grab()
    deadline = time.time() + 60
    while win.index < len(win.plan) and time.time() < deadline:
        app.processEvents()
        time.sleep(0.005)
    win.finish()
    win.close()
    return win.report


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    args = sys.argv[1:]
    keys = [args[0]] if args and args[0] in MODELS else list(MODELS)
    rest = [a for a in args if a not in MODELS]
    scales = [float(x) for x in rest] or list(DEFAULT_SCALES)

    live2d.init()
    app = QApplication(sys.argv[:1])
    reports = {}
    for key in keys:
        reports[key] = run_one(key, scales)
    (OUT_DIR / "all_reports.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    app.quit()
    live2d.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
