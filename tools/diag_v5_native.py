"""诊断：为什么 moc3 v5 的模型在桌面端原生渲染器上画不出来（或只画出一小块）。

手机端（WebGL + Cubism Core JS 5.1）渲染同一个模型是正常的，桌面端
（live2d-py 的 Cubism Native 5.1）一开始几乎是空白，所以先做变量隔离：
    extras   : 是否补装散装的 exp3 / motion3
    resize   : 是否在 LoadModelJson 之后立刻显式 Resize 一次

用法：
    .venv\\Scripts\\python.exe tools\\diag_v5_native.py                 # 默认 360x660 mask=2 v5
    .venv\\Scripts\\python.exe tools\\diag_v5_native.py 360 660 2 v5 1 1
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
V5 = BASE_DIR / "models" / "miku_v5" / "miku.model3.json"
V4 = BASE_DIR / "assets" / "live2d" / "miku" / "miku.model3.json"
OUT_DIR = BASE_DIR / ".tmp" / "diag5"

import OpenGL.GL as gl  # noqa: E402
import live2d.v3 as live2d  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtOpenGLWidgets import QOpenGLWidget  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


class Diag(QOpenGLWidget):
    def __init__(self, path: Path, w: int, h: int, mask: int,
                 extras: bool = False, resize: bool = True) -> None:
        super().__init__()
        self.path = path
        self.mask = mask
        self.extras = extras
        self.explicit_resize = resize
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.resize(w, h)
        self.scale_factor = QGuiApplication.primaryScreen().devicePixelRatio()
        self.model: live2d.LAppModel | None = None
        self.frames = 0
        self.report: dict = {"extras": extras, "explicit_resize": resize, "mask": mask}

    def initializeGL(self) -> None:
        live2d.glInit()
        print(f"[GL] {gl.glGetString(gl.GL_VERSION)}")
        try:
            moc = self.path.parent / json.loads(
                self.path.read_text(encoding="utf-8"))["FileReferences"]["Moc"]
            self.report["moc_consistent"] = live2d.LAppModel().HasMocConsistencyFromFile(str(moc))
        except Exception as exc:  # noqa: BLE001
            print(f"[moc consistency] 调用失败：{exc}")

        self.model = live2d.LAppModel()
        self.model.LoadModelJson(str(self.path), self.mask)
        self.model.SetAutoBlinkEnable(True)
        self.model.SetAutoBreathEnable(True)
        if self.explicit_resize:
            self.model.Resize(int(self.width() * self.scale_factor),
                              int(self.height() * self.scale_factor))
            self.model.SetScale(1.0)
            self.model.SetOffset(0.0, 0.0)
        self.report["canvas_unit"] = list(self.model.GetCanvasSize())
        self.report["canvas_pixel"] = list(self.model.GetCanvasSizePixel())
        self.report["ppu"] = self.model.GetPixelsPerUnit()
        self.report["drawables"] = len(self.model.GetDrawableIds())

        if self.extras:
            for f in sorted(self.path.parent.glob("*.exp3.json")):
                self.model.LoadExtraExpression(f.name[: -len(".exp3.json")], str(f))
            for f in sorted(self.path.parent.glob("*.motion3.json")):
                self.model.LoadExtraMotion("Idle", str(f))
            self.report["expressions"] = list(self.model.GetExpressionIds())
            self.report["motion_groups"] = dict(self.model.GetMotionGroups())
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

    def timerEvent(self, event) -> None:  # noqa: N802
        self.frames += 1
        if self.frames == 40:
            self._measure("scale1")
            self.model.SetScale(0.15)
            return
        if self.frames == 60:
            self._measure("scale015")
            self.model.SetScale(0.35)
            return
        if self.frames == 80:
            self._measure("scale035")
            self.model.SetScale(1.0)
            self.model.SetOffset(1.8, 1.6)   # 大位移：看模型是不是只是跑到视野外了
            return
        if self.frames == 100:
            self._measure("offset_big")
            return
        self.update()

    def _measure(self, label: str) -> None:
        w = int(self.width() * self.scale_factor)
        h = int(self.height() * self.scale_factor)
        self.makeCurrent()
        try:
            raw = gl.glReadPixels(0, 0, w, h, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
        finally:
            self.doneCurrent()
        arr = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 4)
        alpha = arr[:, :, 3]
        nz = int((alpha > 8).sum())
        info = {"pixels_gt8": nz, "alpha_max": int(alpha.max()), "total": w * h}
        if nz:
            rows = np.where(alpha.max(axis=1) > 8)[0]
            cols = np.where(alpha.max(axis=0) > 8)[0]
            info["bbox"] = [
                round(int(cols.min()) / self.scale_factor, 1),
                round((h - 1 - int(rows.max())) / self.scale_factor, 1),
                round(int(cols.max()) / self.scale_factor, 1),
                round((h - 1 - int(rows.min())) / self.scale_factor, 1),
            ]
        self.report[label] = info
        print(f"  [{label}] {json.dumps(info)}")
        self.grabFramebuffer().save(str(OUT_DIR / f"{label}.png"))


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    args = sys.argv[1:]
    w = int(args[0]) if len(args) > 0 else 360
    h = int(args[1]) if len(args) > 1 else 660
    mask = int(args[2]) if len(args) > 2 else 2
    which = args[3] if len(args) > 3 else "v5"
    extras = bool(int(args[4])) if len(args) > 4 else False
    resize = bool(int(args[5])) if len(args) > 5 else True

    live2d.init()
    app = QApplication(sys.argv[:1])
    win = Diag(V5 if which == "v5" else V4, w, h, mask, extras, resize)
    win.show()
    win.grab()
    deadline = time.time() + 25
    while win.frames < 105 and time.time() < deadline:
        app.processEvents()
        time.sleep(0.005)
    (OUT_DIR / f"report_{which}_{w}x{h}_m{mask}_e{int(extras)}_r{int(resize)}.json").write_text(
        json.dumps(win.report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(win.report, ensure_ascii=False, indent=2))
    win.close()
    app.quit()
    live2d.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
