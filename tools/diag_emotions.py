"""诊断：走真实的 Live2DView.set_emotion()，逐个情感截图，确认不再出现表情重叠。

输出 .diag/emotion_grid.png
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from PIL import Image, ImageDraw
import live2d.v3 as live2d
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from ui.live2d_view import Live2DView

OUT = BASE / ".diag"
OUT.mkdir(exist_ok=True)
W, H = 400, 580
EMOTIONS = ["NORMAL", "HAPPY", "SAD", "ANGRY", "SURPRISED", "MOTIVATED", "EMPATHY"]


def main() -> int:
    live2d.init()
    app = QApplication([])
    view = Live2DView(BASE / "assets" / "live2d" / "miku" / "miku.model3.json",
                      fps=60, framing_scale=0.9, framing_offset=(0.0, 0.45))
    view.resize(W, H)
    view.show()

    st = {"i": -1, "phase": "settle", "ticks": 0, "shots": {}}

    def tick():
        if st["i"] >= 0 and st["phase"] == "measure":
            name = EMOTIONS[st["i"]]
            p = OUT / f"emo_{name}.png"
            view.grabFramebuffer().save(str(p))
            st["shots"][name] = p
            st["phase"] = "settle"
            st["ticks"] = 0
            return
        st["ticks"] += 1
        if st["phase"] == "settle" and st["ticks"] >= 40:   # ~1.6s，等动作+表情稳定
            st["i"] += 1
            if st["i"] >= len(EMOTIONS):
                app.quit()
                return
            view.set_emotion(EMOTIONS[st["i"]])
            st["phase"] = "measure"
            st["ticks"] = 0
        view.update()

    t = QTimer()
    t.timeout.connect(tick)
    t.start(40)
    app.exec()

    CROP = (100, 100, 300, 280)
    cw, ch = CROP[2] - CROP[0], CROP[3] - CROP[1]
    scale = 2
    cols = 4
    rows = 2
    grid = Image.new("RGB", (cw * scale * cols, (ch * scale + 22) * rows), (250, 250, 250))
    draw = ImageDraw.Draw(grid)
    for idx, name in enumerate(EMOTIONS):
        p = st["shots"].get(name)
        if not p or not Path(p).exists():
            continue
        im = Image.open(p).convert("RGB").crop(CROP).resize((cw * scale, ch * scale), Image.NEAREST)
        cx = (idx % cols) * cw * scale
        cy = (idx // cols) * (ch * scale + 22)
        grid.paste(im, (cx, cy + 22))
        draw.text((cx + 6, cy + 6), f"{idx}: {name}", fill=(200, 0, 0))
    out = OUT / "emotion_grid.png"
    grid.save(out)
    print("grid ->", out)
    live2d.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
