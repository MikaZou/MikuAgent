"""诊断：逐个应用 6 个 exp3 表情，对比渲染结果，找出哪个表情有问题。

输出 .diag/expr_grid.png —— 一行一个状态（无表情 + 6 个表情 + 重置）。
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
from PIL import Image, ImageDraw
import live2d.v3 as live2d
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication
from PySide6.QtOpenGLWidgets import QOpenGLWidget

MODEL = BASE / "assets" / "live2d" / "miku" / "miku.model3.json"
OUT = BASE / ".diag"
OUT.mkdir(exist_ok=True)
W, H = 460, 680

STATES = ["(none)", "Chijing", "Dazhihui", "Mimiyan", "Saihong", "Yanjing", "liuhan", "RESET"]


class Probe(QOpenGLWidget):
    def __init__(self):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self.resize(W, H)
        self.m = None
        self.ids = []

    def initializeGL(self):
        live2d.glInit()
        self.m = live2d.LAppModel()
        self.m.LoadModelJson(str(MODEL))
        self.m.SetAutoBlinkEnable(False)   # 关掉眨眼，避免干扰对比
        self.m.SetAutoBreathEnable(False)
        self.m.SetScale(1.0)
        self.m.SetOffset(0.0, 0.45)
        self.ids = list(self.m.GetExpressionIds())
        print("expressions:", self.ids)

    def resizeGL(self, w, h):
        self.m.Resize(w, h)
        self.m.SetScale(1.0)
        self.m.SetOffset(0.0, 0.45)

    def paintGL(self):
        live2d.clearBuffer(0, 0, 0, 0)
        self.m.Update()
        self.m.Draw()

    def snapshot(self, tag):
        f = OUT / f"expr_{tag}.png"
        self.grabFramebuffer().save(str(f))
        return f


def main():
    live2d.init()
    app = QApplication([])
    w = Probe()
    w.show()

    st = {"i": -1, "phase": "settle", "ticks": 0, "shots": {}}

    def tick():
        if st["i"] >= 0 and st["phase"] == "measure":
            tag = STATES[st["i"]].replace("(", "").replace(")", "")
            st["shots"][STATES[st["i"]]] = w.snapshot(tag)
            st["phase"] = "settle"
            st["ticks"] = 0
            return
        st["ticks"] += 1
        if st["phase"] == "settle" and st["ticks"] >= 30:   # ~1.2s，等表情淡入完成
            st["i"] += 1
            if st["i"] >= len(STATES):
                app.quit()
                return
            name = STATES[st["i"]]
            if name == "(none)" or name == "RESET":
                w.m.ResetExpression()
            else:
                w.m.SetExpression(name)
            st["phase"] = "measure"
            st["ticks"] = 0
        w.update()

    t = QTimer()
    t.timeout.connect(tick)
    t.start(40)
    app.exec()

    # 拼图：每格取脸部区域
    CROP = (120, 130, 340, 320)   # (l, t, r, b)
    cw, ch = CROP[2] - CROP[0], CROP[3] - CROP[1]
    scale = 2
    cols, rows = 4, 2
    grid = Image.new("RGB", (cw * scale * cols, (ch * scale + 22) * rows), (250, 250, 250))
    draw = ImageDraw.Draw(grid)
    for idx, name in enumerate(STATES):
        p = st["shots"].get(name)
        if not p or not Path(p).exists():
            continue
        im = Image.open(p).convert("RGB").crop(CROP).resize((cw * scale, ch * scale), Image.NEAREST)
        cx = (idx % cols) * cw * scale
        cy = (idx // cols) * (ch * scale + 22)
        grid.paste(im, (cx, cy + 22))
        draw.text((cx + 6, cy + 6), f"{idx}: {name}", fill=(200, 0, 0))
    out = OUT / "expr_grid.png"
    grid.save(out)
    print("grid ->", out)
    live2d.dispose()


if __name__ == "__main__":
    main()
