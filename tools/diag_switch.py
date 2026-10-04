"""诊断：同一个进程里「换模型」到底行不行。

背景：桌面端要支持在 v4（assets/live2d/miku）与 v5（models/miku_v5）之间切换，
但探针发现**先加载 v4 再加载 v5，v5 会画不出来**（单独加载 v5 一切正常）。
如果这是真的，切换模型就必须换进程 —— 那 UX 与实现完全不同，所以要先钉死。

本脚本在**同一个 GL 窗口**里依次加载 A → 释放 → 加载 B，每一步都量 alpha 像素数。

用法：
    .venv\\Scripts\\python.exe tools\\diag_switch.py            # v4 -> v5
    .venv\\Scripts\\python.exe tools\\diag_switch.py v5 v4      # v5 -> v4
"""
from __future__ import annotations

import gc
import json
import os
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PATHS = {
    "v4": BASE_DIR / "assets" / "live2d" / "miku" / "miku.model3.json",
    "v5": BASE_DIR / "models" / "miku_v5" / "miku.model3.json",
}
OUT_DIR = BASE_DIR / ".tmp" / "switch"

import OpenGL.GL as gl  # noqa: E402
import live2d.v3 as live2d  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtOpenGLWidgets import QOpenGLWidget  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


class Switch(QOpenGLWidget):
    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle("switch diag")
        self.resize(360, 660)
        self.scale_factor = QGuiApplication.primaryScreen().devicePixelRatio()
        self.order = order
        self.step = 0
        self.model: live2d.LAppModel | None = None
        self.frames = 0
        self.report: list[dict] = []
        self._pending: str | None = None

    # ------------------------------------------------------------- OpenGL
    def initializeGL(self) -> None:
        live2d.glInit()
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

    # ------------------------------------------------------------- 装配 / 卸载
    def _load(self, key: str) -> None:
        print(f"[load] {key}")
        self.model = live2d.LAppModel()
        self.model.LoadModelJson(str(PATHS[key]))
        self.model.SetAutoBlinkEnable(True)
        self.model.SetAutoBreathEnable(True)
        self.model.Resize(int(self.width() * self.scale_factor),
                          int(self.height() * self.scale_factor))
        self.model.SetScale(1.0)
        self.model.SetOffset(0.0, 0.0)

    def _unload(self) -> None:
        """完整释放：先销毁渲染器，再丢掉 Python 引用并强制 GC。

        顺序不能反：CubismRenderer 持有 GL 纹理，必须在 GL 上下文还活着时销毁；
        靠 GC 顺带销毁的话时机不可控，很容易把 GL 调用拖到上下文之外。
        """
        if self.model is not None:
            try:
                self.model.DestroyRenderer()
            except Exception as exc:  # noqa: BLE001
                print(f"[unload] DestroyRenderer 失败：{exc}")
        self.model = None
        gc.collect()
        print("[unload] 已释放，GC 完成")

    # -------------------------------------------------------------- 帧循环
    def timerEvent(self, event) -> None:  # noqa: N802
        self.frames += 1
        f = self.frames

        # 每次换模型：第 20 帧装配，第 45 帧量，第 50 帧拆
        base = 20 + self.step * 40
        if f == base:
            self._load(self.order[self.step])
            return
        if f == base + 25:
            self._measure(f"{self.order[self.step]}_after_load")
            return
        if f == base + 30:
            self._unload()
            self.step += 1
            if self.step >= len(self.order):
                # 全部走完，最后再装一次第一个模型，验证「换回来」也可以
                self._load(self.order[0])
                self.step = 99
            return
        if self.step == 99 and f == base + 55:
            self._measure("reload_first")
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
        info = {"label": label, "pixels_gt8": nz}
        if nz:
            rows = np.where(alpha.max(axis=1) > 8)[0]
            cols = np.where(alpha.max(axis=0) > 8)[0]
            info["bbox"] = [
                round(int(cols.min()) / self.scale_factor, 1),
                round((h - 1 - int(rows.max())) / self.scale_factor, 1),
                round(int(cols.max()) / self.scale_factor, 1),
                round((h - 1 - int(rows.min())) / self.scale_factor, 1),
            ]
        self.report.append(info)
        print(f"  [{label}] {json.dumps(info)}")
        self.grabFramebuffer().save(str(OUT_DIR / f"{label}.png"))


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    args = sys.argv[1:]
    order = [a for a in args if a in PATHS] or ["v4", "v5"]

    live2d.init()
    app = QApplication(sys.argv[:1])
    win = Switch(order)
    win.show()
    win.grab()
    deadline = time.time() + 40
    while win.step != 99 and time.time() < deadline:
        app.processEvents()
        time.sleep(0.005)
    # 等最后一档量完
    while len(win.report) < len(order) + 1 and time.time() < deadline:
        app.processEvents()
        time.sleep(0.005)
    print("\n=== SUMMARY ===")
    print(json.dumps(win.report, ensure_ascii=False, indent=2))
    (OUT_DIR / "report.json").write_text(
        json.dumps({"order": order, "report": win.report}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    win.close()
    app.quit()
    live2d.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
