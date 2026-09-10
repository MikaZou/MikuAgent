"""P0 冒烟验证：live2d-py + PySide6 原生透明窗口渲染 Miku。

验证目标（决定整个原生化方案是否成立）：
  1. QOpenGLWidget + WA_TranslucentBackground 能否真逐像素透明
  2. live2d.v3 能否加载本项目的 Miku 模型（moc3 v4 / model3.json v3）
  3. 命名表情（exp3）与动作组是否可用
  4. 无边框 + 置顶 + 拖拽 + 按像素命中检测

用法：
    .venv\\Scripts\\python.exe tools\\smoke_live2d.py            # 交互式看窗口
    .venv\\Scripts\\python.exe tools\\smoke_live2d.py --selftest # 自动跑 6 秒存图退出
"""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
MODEL_PATH = BASE_DIR / "assets" / "live2d" / "miku" / "miku.model3.json"

import OpenGL.GL as gl
import live2d.v3 as live2d
from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QApplication


class SmokeWindow(QOpenGLWidget):
    def __init__(self, selftest: bool = False) -> None:
        super().__init__()
        self.selftest = selftest
        self.model: live2d.LAppModel | None = None
        self.frame = 0
        self._facts: dict = {}
        self._dragging = False
        self._drag_origin = None
        self._in_model = False

        self.setWindowTitle("MikuAgent P0 smoke")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.resize(420, 560)
        self.scale_factor = QGuiApplication.primaryScreen().devicePixelRatio()

    # ---------- OpenGL ----------
    def initializeGL(self) -> None:
        live2d.glInit()
        vendor = gl.glGetString(gl.GL_VENDOR)
        renderer = gl.glGetString(gl.GL_RENDERER)
        version = gl.glGetString(gl.GL_VERSION)
        print(f"[GL] vendor   : {vendor}")
        print(f"[GL] renderer : {renderer}")
        print(f"[GL] version  : {version}")

        self.model = live2d.LAppModel()
        self.model.LoadModelJson(str(MODEL_PATH))

        self._facts["expressions"] = list(self.model.GetExpressionIds())
        self._facts["motion_groups"] = list(self.model.GetMotionGroups())
        self._facts["part_count"] = self.model.GetPartCount()
        self._facts["param_count"] = self.model.GetParameterCount()
        self._facts["canvas"] = self.model.GetCanvasSize()

        self.model.SetAutoBlinkEnable(True)
        self.model.SetAutoBreathEnable(True)
        self.model.SetScale(1.0)

        self.startTimer(16)  # ~60fps

    def resizeGL(self, w: int, h: int) -> None:
        if self.model:
            self.model.Resize(w, h)

    def paintGL(self) -> None:
        live2d.clearBuffer()  # 全透明清屏
        if self.model is None:
            return
        self.model.Update()
        self.model.Draw()

    # ---------- 帧循环 ----------
    def timerEvent(self, event) -> None:
        if not self.isVisible():
            return
        self.frame += 1

        # 视线跟随鼠标
        if self.model is not None:
            gx = QCursor.pos().x() - self.x()
            gy = QCursor.pos().y() - self.y()
            self.model.Drag(gx, gy)

        # 自检脚本：跑一遍表情与动作，确认不抛异常
        if self.selftest:
            if self.frame == 30 and "Saihong" in self._facts.get("expressions", []):
                self.model.SetExpression("Saihong")
                self._facts["expression_set"] = "Saihong"
            if self.frame == 90:
                self.model.SetExpression("Chijing")
                self._facts["expression_set_2"] = "Chijing"
            if self.frame == 150:
                self.model.ResetExpression()
                self.model.StartMotion("Tap", 0, live2d.MotionPriority.FORCE)
                self._facts["motion_started"] = "Tap"

        self.update()

    # ---------- 命中检测（按像素 alpha） ----------
    def _alpha_at(self, x: float, y: float) -> int:
        h = self.height()
        px = int(x * self.scale_factor)
        py = int((h - y) * self.scale_factor)
        if px < 0 or py < 0 or px >= self.width() or py >= self.height():
            return 0
        data = gl.glReadPixels(px, py, 1, 1, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
        return data[3]

    def _in_model(self, x: float, y: float) -> bool:
        return self._alpha_at(x, y) > 0

    # ---------- 鼠标 ----------
    def mousePressEvent(self, event) -> None:
        x, y = event.position().x(), event.position().y()
        if self._in_model(x, y):
            self._in_model_pressed = True
            self._drag_origin = (x, y)
            self._win_origin = (self.x(), self.y())
            print(f"[click] hit model at ({x:.0f},{y:.0f})")
        else:
            self._in_model_pressed = False

    def mouseMoveEvent(self, event) -> None:
        if getattr(self, "_in_model_pressed", False):
            x, y = event.position().x(), event.position().y()
            dx = x - self._drag_origin[0]
            dy = y - self._drag_origin[1]
            self.move(int(self._win_origin[0] + dx), int(self._win_origin[1] + dy))

    def mouseReleaseEvent(self, event) -> None:
        if getattr(self, "_in_model_pressed", False):
            self.model.StartRandomMotion(priority=live2d.MotionPriority.NORMAL)
        self._in_model_pressed = False

    # ---------- 自检收尾 ----------
    def finish_selftest(self, out_png: str) -> None:
        img = self.grabFramebuffer()
        img.save(out_png)
        print("\n=== P0 FACTS ===")
        for k, v in self._facts.items():
            print(f"  {k}: {v}")
        print(f"  screenshot: {out_png}")


def main() -> int:
    selftest = "--selftest" in sys.argv
    if not MODEL_PATH.exists():
        print(f"[FATAL] model not found: {MODEL_PATH}")
        return 2

    live2d.init()
    app = QApplication(sys.argv)
    win = SmokeWindow(selftest=selftest)
    win.show()

    if selftest:
        out = str(BASE_DIR / ".tmp" / "p0_smoke.png")
        os.makedirs(os.path.dirname(out), exist_ok=True)

        from PySide6.QtCore import QTimer

        QTimer.singleShot(3000, lambda: win.finish_selftest(out))
        QTimer.singleShot(3200, app.quit)

    code = app.exec()
    live2d.dispose()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
