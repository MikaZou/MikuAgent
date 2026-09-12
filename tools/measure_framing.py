"""测量模型在指定窗口尺寸下的逻辑包围盒，用于精确摆放气泡与模型。

用法:
    python tools/measure_framing.py [宽] [高]

输出每行：(scale, dy) -> 模型 top/bottom/宽/高（逻辑像素），
并标出能完整落在「气泡下方 ~ 输入栏上方」安全区里的配置。
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
OUT = BASE / ".diag"
OUT.mkdir(exist_ok=True)

import numpy as np
import live2d.v3 as live2d
from PIL import Image
from PySide6.QtCore import Qt, QTimer
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QApplication

MODEL = BASE / "assets" / "live2d" / "miku" / "miku.model3.json"
TMP = OUT / "bbox_probe.png"

W = int(sys.argv[1]) if len(sys.argv) > 1 else 400
H = int(sys.argv[2]) if len(sys.argv) > 2 else 580

# 垂直分区（与 ui/pet_window.py 保持一致）
BUBBLE_TOP = 46
BUBBLE_MAX_H = 188
SAFE_TOP = BUBBLE_TOP + BUBBLE_MAX_H + 6
SAFE_BOTTOM = H - 76     # 输入栏顶边

CONFIGS = [(s, dy) for s in (0.70, 0.75, 0.80, 0.85, 0.90) for dy in (0.0, 0.15, 0.30)]


class Probe(QOpenGLWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self.resize(W, H)
        self.m = None

    def initializeGL(self):
        live2d.glInit()
        self.m = live2d.LAppModel()
        self.m.LoadModelJson(str(MODEL))
        self.m.SetAutoBlinkEnable(False)
        self.m.SetAutoBreathEnable(False)

    def resizeGL(self, w, h):
        if self.m:
            self.m.Resize(w, h)

    def paintGL(self):
        live2d.clearBuffer(0, 0, 0, 0)
        if self.m:
            self.m.Update()
            self.m.Draw()

    def bbox(self):
        self.grabFramebuffer().save(str(TMP))
        arr = np.array(Image.open(TMP).convert("RGBA"))[:, :, 3]
        ys, xs = np.where(arr > 8)
        if len(xs) == 0:
            return None
        dpr = arr.shape[1] / W
        return (int(xs.min() / dpr), int(ys.min() / dpr),
                int(xs.max() / dpr), int(ys.max() / dpr))


def main() -> int:
    live2d.init()
    app = QApplication([])
    w = Probe()
    w.show()
    st = {"i": -1, "phase": "settle", "ticks": 0}

    print(f"窗口 {W}x{H}   安全区 y in [{SAFE_TOP}, {SAFE_BOTTOM}]")
    print(f"{'scale':>6} {'dy':>6} | {'top':>5} {'bottom':>7} {'宽':>5} {'高':>5} | 是否合适")
    print("-" * 62)

    def tick():
        if st["i"] >= 0 and st["phase"] == "measure":
            b = w.bbox()
            sc, dy = CONFIGS[st["i"]]
            if b:
                top, bot, width, height = b[1], b[3], b[2] - b[0], b[3] - b[1]
                if top >= SAFE_TOP and bot <= SAFE_BOTTOM:
                    mark = "OK 完全避开"
                else:
                    mark = f"溢出(顶缺{max(0, SAFE_TOP - top)}/底超{max(0, bot - SAFE_BOTTOM)})"
                print(f"{sc:6.2f} {dy:6.2f} | {top:>5} {bot:>7} {width:>5} {height:>5} | {mark}", flush=True)
            else:
                print(f"{sc:6.2f} {dy:6.2f} | EMPTY", flush=True)
            st["phase"] = "settle"
            st["ticks"] = 0
            return
        st["ticks"] += 1
        if st["phase"] == "settle" and st["ticks"] >= 3:
            st["i"] += 1
            if st["i"] >= len(CONFIGS):
                app.quit()
                return
            sc, dy = CONFIGS[st["i"]]
            w.m.SetScale(sc)
            w.m.SetOffset(0.0, dy)
            st["phase"] = "measure"
            st["ticks"] = 0
        w.update()

    t = QTimer()
    t.timeout.connect(tick)
    t.start(30)
    app.exec()
    live2d.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
