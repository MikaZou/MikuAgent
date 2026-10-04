"""测量模型在指定窗口尺寸下的取景结果与安全区落位。

用法:
    .venv\\Scripts\\python.exe tools/measure_framing.py [宽] [高] [模型id]

背景变化（重要）：桌面端以前是**查表**取景的 —— 按窗口高度查一张手工量好的
(气泡高, scale, dy) 表。那张表只对经典模型成立，加第二个模型（moc3 v5，
美术超出画布）就完全跑偏。现在 `Live2DView.auto_frame()` 会实测取景：
画几帧、读回 alpha 包围盒、反推 scale 与 offset。

所以这个工具也换了职责：不再扫描 (scale, dy) 组合去挑一组能用的，
而是**复述自动取景实际算出了什么、落位是否符合预期** —— 改布局常量、
换窗口尺寸、加新模型之后，用它一眼确认没有压到气泡或输入栏。

输出：
    .diag/framing_<模型id>.png   取景结果截图
    控制台：安全区、实测包围盒、溢出量
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "backend"))

OUT = BASE / ".diag"
OUT.mkdir(exist_ok=True)

import OpenGL.GL as gl  # noqa: E402
import live2d.v3 as live2d  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
import models_catalog  # noqa: E402
from ui.pet_window import (  # noqa: E402
    BAR_H, BAR_MARGIN, BUBBLE_GAP, BUBBLE_MAX, BUBBLE_MIN, BUBBLE_RATIO, BUBBLE_TOP,
)
from ui.live2d_view import Live2DView  # noqa: E402

W = int(sys.argv[1]) if len(sys.argv) > 1 else config.WINDOW_WIDTH
H = int(sys.argv[2]) if len(sys.argv) > 2 else config.WINDOW_HEIGHT
MODEL_ID = sys.argv[3] if len(sys.argv) > 3 else models_catalog.selected("desktop")


def safe_area() -> QRect:
    """与 PetWindow._avail_rect() 同一套算法（气泡下沿 ~ 输入栏上沿）。"""
    bubble_max = int(max(BUBBLE_MIN, min(BUBBLE_MAX, H * BUBBLE_RATIO)))
    top = BUBBLE_TOP + bubble_max + BUBBLE_GAP
    bottom = H - BAR_H - BAR_MARGIN - BUBBLE_GAP
    return QRect(16, top, W - 32, max(1, bottom - top))


def main() -> int:
    live2d.init()
    app = QApplication(sys.argv[:1])
    view = Live2DView(MODEL_ID, fps=60)
    view.resize(W, H)
    view.show()
    end = time.time() + 1.5
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)

    area = safe_area()
    ok = view.auto_frame(area)
    end = time.time() + 0.4
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)

    box = view.model_box
    print(f"\n模型        : {MODEL_ID}  ({models_catalog.resolve(MODEL_ID)['name']})")
    print(f"窗口        : {W}x{H}")
    print(f"安全区      : x {area.left()}..{area.right()}  y {area.top()}..{area.bottom()}")
    print(f"自动取景    : {'成功' if ok else '失败'}"
          f"  scale={view.framing_scale:.4f} offset=({view.framing_offset[0]:.4f}, {view.framing_offset[1]:.4f})")
    if not box:
        print("实测包围盒  : 无（模型没画出来？）")
        return 1
    rect = [round(v) for v in box]
    print(f"实测包围盒  : x {rect[0]}..{rect[2]}  y {rect[1]}..{rect[3]}"
          f"  ({rect[2] - rect[0]}x{rect[3] - rect[1]})")
    over = {
        "左": area.left() - box[0], "上": area.top() - box[1],
        "右": box[2] - area.right(), "下": box[3] - area.bottom(),
    }
    worst = max(over.values())
    print("溢出安全区  : " + "  ".join(f"{k} {v:+.0f}" for k, v in over.items())
          + f"   → 最大 {worst:+.0f}px")
    if worst > 8:
        print("⚠️  溢出超过 8px：动画中间态可能压到气泡或输入栏，考虑缩小安全区或调整常量")
    else:
        print("✅ 落位正常（动画中间态允许几像素溢出，安全带外还留着 BUBBLE_GAP 余量）")

    png = OUT / f"framing_{MODEL_ID}.png"
    view.grabFramebuffer().save(str(png))
    print(f"截图        : {png}")
    view.shutdown()
    view.close()
    app.quit()
    live2d.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
