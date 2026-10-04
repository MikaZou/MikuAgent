"""诊断：走真实的 Live2DView.set_emotion()，逐个情感截图，确认不再出现表情重叠。

用法：
    .venv\\Scripts\\python.exe tools/diag_emotions.py [模型id]
默认用设置里选中的桌面端模型；情绪→表情的映射来自模型画像
（backend/models_catalog.py），所以换模型后这个工具测的就是那个模型自己的映射。

输出 .diag/emotion_grid.png
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "backend"))

import models_catalog
from PIL import Image, ImageDraw
import live2d.v3 as live2d
from PySide6.QtCore import QRect, QTimer
from PySide6.QtWidgets import QApplication

from ui.live2d_view import Live2DView

OUT = BASE / ".diag"
OUT.mkdir(exist_ok=True)
W, H = 400, 580
MODEL_ID = sys.argv[1] if len(sys.argv) > 1 else models_catalog.selected("desktop")
EMOTIONS = ["NORMAL", "HAPPY", "SAD", "ANGRY", "SURPRISED", "MOTIVATED", "EMPATHY"]


def main() -> int:
    live2d.init()
    app = QApplication([])
    view = Live2DView(MODEL_ID, fps=60)
    view.resize(W, H)
    view.show()

    # 先跑自动取景，再按实测包围盒决定截图裁哪一块。
    # 不能写死裁切框：两个模型的取景尺寸差很多（实测经典 ~57x345、
    # 新模型 ~155x344），写死的框在新模型上会裁到空白。
    import time as _time
    from PySide6.QtCore import QRect
    end = _time.time() + 1.5
    while _time.time() < end:
        app.processEvents()
        _time.sleep(0.01)
    view.auto_frame(QRect(16, 150, W - 32, H - 240))
    end = _time.time() + 0.4
    while _time.time() < end:
        app.processEvents()
        _time.sleep(0.01)

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

    # 脸部裁切框：从实测包围盒的顶部往下取一小块（头在美术包围盒最上面）
    box = view.model_box or [100, 100, 300, 460]
    face_h = max(60, int((box[3] - box[1]) * 0.30))
    cx = int((box[0] + box[2]) / 2)
    half = max(60, int((box[2] - box[0]) * 0.7))
    CROP = (max(0, cx - half), max(0, int(box[1]) - 6),
            min(W, cx + half), min(H, int(box[1]) + face_h))
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
