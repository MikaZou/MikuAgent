"""诊断 2：列出全部参数；测试 Idle 动作是否会循环、以及动作结束后会怎样。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import live2d.v3 as live2d
from PySide6.QtCore import Qt, QTimer
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QApplication

MODEL = BASE / "assets" / "live2d" / "miku" / "miku.model3.json"


class Probe(QOpenGLWidget):
    def __init__(self):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self.resize(360, 600)
        self.m = None
        self.ids = []
        self.n = 0
        self.log = []
        self.snapshots = []

    def initializeGL(self):
        live2d.glInit()
        self.m = live2d.LAppModel()
        self.m.LoadModelJson(str(MODEL))
        self.m.SetAutoBlinkEnable(True)
        self.m.SetAutoBreathEnable(True)
        for i in range(self.m.GetParameterCount()):
            self.ids.append(self.m.GetParameter(i).id)
        self.startTimer(16)

    def paintGL(self):
        live2d.clearBuffer(0, 0, 0, 0)
        if self.m:
            self.m.Update()
            self.m.Draw()

    def val(self, pid):
        try:
            return round(float(self.m.GetParameterValue(pid)), 3)
        except Exception:
            return None

    def timerEvent(self, e):
        self.n += 1
        t = self.n / 60.0
        # 第 1 秒：不播动作，记录基准姿态
        if self.n == 60:
            self.base = {p: self.val(p) for p in self.ids}
        # 第 1.0 秒：启动一个 Idle 动作
        if self.n == 60:
            self.m.StartRandomMotion(group="Idle", priority=live2d.MotionPriority.IDLE)
            self.log.append("t=1.0s  StartRandomMotion(Idle, IDLE)")
        # 每秒记录一次动作是否结束 + 若干身体参数
        if self.n % 60 == 0 and self.n >= 60:
            fin = None
            try:
                fin = self.m.IsMotionFinished()
            except Exception as exc:
                fin = f"err:{exc}"
            snap = {p: self.val(p) for p in
                    ("PARAM_ANGLE_X", "PARAM_ANGLE_Y", "PARAM_ANGLE_Z",
                     "PARAM_BODY_ANGLE_Z", "PARAM_BREATH", "PARAM_EYE_L_OPEN")}
            self.snapshots.append((round(t, 1), fin, snap))
        self.update()


def main() -> int:
    live2d.init()
    app = QApplication([])
    w = Probe()
    w.show()
    QTimer.singleShot(8000, app.quit)
    app.exec()

    print("=== 全部参数 ID（共 %d 个）===" % len(w.ids))
    for i in range(0, len(w.ids), 4):
        print("   " + "  ".join(f"{x:<26}" for x in w.ids[i:i+4]))
    print()
    for line in w.log:
        print(line)
    print("\n=== Idle 动作启动后的状态（每 1 秒）===")
    print(f"{'t':>5} {'动作结束?':>9}  身体参数")
    for t, fin, snap in w.snapshots:
        s = "  ".join(f"{k.replace('PARAM_','')}={v}" for k, v in snap.items() if v is not None)
        print(f"{t:>5} {str(fin):>9}  {s}")
    live2d.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
