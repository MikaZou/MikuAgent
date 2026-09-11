"""决定性测试：QOpenGLWidget 的子控件能否绘制在 GL 内容之上。

桌面桌宠里气泡是 QOpenGLWidget 的子控件，而模型是 GL 内容。
如果子控件压在 GL 上面，说明合成正常，遮挡就是纯坐标问题；
反之必须从布局上彻底避免重叠。

跑完会在屏幕上显示一个窗口，并把截图存到 .diag/zorder.png。
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import live2d.v3 as live2d
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QApplication, QLabel

MODEL = BASE / "assets" / "live2d" / "miku" / "miku.model3.json"
OUT = BASE / ".diag"
OUT.mkdir(exist_ok=True)
W, H = 400, 580


class Win(QOpenGLWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # 关键：Live2DView 里设了这个属性。它让 GL 内容无视正常层叠顺序、
        # 永远画在最上面 —— 子控件反而会被压住。用它复现线上现象。
        if "--stack-on-top" in sys.argv:
            self.setAttribute(Qt.WidgetAttribute.WA_AlwaysStackOnTop, True)
            print(">>> 已设置 WA_AlwaysStackOnTop = True")
        else:
            print(">>> 未设置 WA_AlwaysStackOnTop（默认层叠）")
        self.resize(W, H)
        self.move(100, 100)
        self.m = None

        # 半透明红块（模拟气泡）横跨模型头部区域
        self.overlay = QLabel("子控件测试子控件测试", self)
        self.overlay.setStyleSheet(
            "background: rgba(255,0,0,255); color: white; font-size: 20px;"
        )
        self.overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.overlay.setGeometry(20, 60, W - 40, 100)

    def initializeGL(self):
        live2d.glInit()
        self.m = live2d.LAppModel()
        self.m.LoadModelJson(str(MODEL))
        self.m.SetAutoBlinkEnable(False)
        self.m.SetAutoBreathEnable(False)
        self.m.SetScale(0.9)
        self.m.SetOffset(0.0, 0.45)
        self.startTimer(16)

    def resizeGL(self, w, h):
        if self.m:
            self.m.Resize(w, h)

    def paintGL(self):
        live2d.clearBuffer(0, 0, 0, 0)
        if self.m:
            self.m.Update()
            self.m.Draw()

    def timerEvent(self, e):
        if self.m:
            local = self.mapFromGlobal(QCursor.pos())
            self.m.Drag(local.x(), local.y())
        self.update()


def main() -> int:
    live2d.init()
    app = QApplication([])
    w = Win()
    w.show()
    w.overlay.raise_()   # 显式提到最上层
    QTimer.singleShot(6000, app.quit)
    app.exec()
    live2d.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
