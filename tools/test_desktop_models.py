"""回归：桌面端两个模型都渲染得出来，且自动取景把它们放进安全带里。

为什么必须是**真 GL 窗口**跑：取景是靠读 framebuffer 的 alpha 实测的，
offscreen 拿不到真实读数；而且「先加载 A 再换 B」这类问题只在真渲染器上复现
（实测踩到：换模型时 LoadModelJson 跑在没有当前 GL 上下文的情况下，
新模型投影坏掉，取景只能纵向填满、横向只剩 1/3）。

用法：.venv\\Scripts\\python.exe tools\\test_desktop_models.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "backend"))

import OpenGL.GL as gl  # noqa: E402
import live2d.v3 as live2d  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import models_catalog  # noqa: E402
from ui.live2d_view import Live2DView  # noqa: E402

WIN_W, WIN_H = 360, 660
# 与 PetWindow._avail_rect() 同一条安全带（660 高时的取值：
# 顶部 46+184+8=238，底部 660-58-12-8=582）
AVAIL = QRect(16, 238, 328, 344)
OUT_DIR = BASE_DIR / ".tmp" / "desktop_models"

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILED.append(name)


def pump(app, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def alpha_box(view: Live2DView):
    w = int(view.width() * view.devicePixelRatioF())
    h = int(view.height() * view.devicePixelRatioF())
    view.makeCurrent()
    try:
        raw = gl.glReadPixels(0, 0, w, h, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
    finally:
        view.doneCurrent()
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 4)
    alpha = arr[:, :, 3]
    nz = int((alpha > 8).sum())
    if not nz:
        return 0, None
    rows = np.where(alpha.max(axis=1) > 8)[0]
    cols = np.where(alpha.max(axis=0) > 8)[0]
    dpr = view.devicePixelRatioF()
    return nz, [
        int(cols.min()) / dpr, (h - 1 - int(rows.max())) / dpr,
        int(cols.max()) / dpr, (h - 1 - int(rows.min())) / dpr,
    ]


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    live2d.init()
    app = QApplication(sys.argv[:1])

    view = Live2DView("miku_v5", fps=60)
    view.resize(WIN_W, WIN_H)
    view.show()
    pump(app, 1.5)

    for model_id in ("miku_v5", "miku", "miku_v5"):
        print(f"\n=== {model_id} ===")
        if view.model_id != model_id:
            ok = view.load_model(model_id)
            pump(app, 0.6)
        else:
            ok = view.model is not None
        check(f"{model_id} 模型已加载", ok and view.model is not None)
        if view.model is None:
            continue
        check(f"{model_id} drawable/参数非零",
              view.model_info.get("drawables", 0) > 0 and view.model_info.get("params", 0) > 0,
              f"{view.model_info.get('drawables')} drawable / {view.model_info.get('params')} 参数")
        if model_id == "miku_v5":
            check("新模型补装出了表情", len(view.expression_ids) >= 8,
                  f"{len(view.expression_ids)} 张 {view.expression_ids}")
            check("新模型补装出了动作组", bool(view.motion_groups), str(view.motion_groups))

        # 画像里引用的每个表情/动作组都必须在模型里真的存在，否则「换模型后
        # 表情不生效」这种问题只会表现为「她不笑」，很难归因。
        prof = models_catalog.resolve(model_id)["profile"]
        wanted_exp = {v for v in (prof.get("emotion_expression") or {}).values() if v}
        wanted_mot = {v for v in (prof.get("emotion_motion") or {}).values() if v}
        missing_exp = sorted(wanted_exp - set(view.expression_ids))
        missing_mot = sorted(wanted_mot - set(view.motion_groups))
        check(f"{model_id} 画像引用的表情都存在", not missing_exp, f"缺 {missing_exp}")
        check(f"{model_id} 画像引用的动作组都存在", not missing_mot, f"缺 {missing_mot}")

        # 待机调度必须能真的转起来。判据是「有没有真的播过一个动作」——
        # 不能只看 IsMotionFinished()：刚加载的模型这个接口返回 False
        # （动作管理器还没启动过），照字面理解就是「永远在忙」。
        deadline = time.time() + 8.0
        while time.time() < deadline and not getattr(view, "_played_any", False):
            pump(app, 0.25)
        check(f"{model_id} 待机动作真的播起来了", bool(getattr(view, "_played_any", False)),
              f"_played_any={getattr(view, '_played_any', None)}")

        fit = view.auto_frame(AVAIL)
        pump(app, 0.4)
        check(f"{model_id} 自动取景成功", fit,
              f"scale={view.framing_scale:.4f} offset={view.framing_offset}")

        nz, box = alpha_box(view)
        check(f"{model_id} 画出了像素", nz > 3000, f"{nz} 个 alpha>8 的像素")
        if box:
            # 容差 8px：取景是在「停掉动作」的静止姿态上量的，随后待机动画一动
            # （手臂/头发/点头）美术包围盒本来就会比静止时大几个像素。
            # 安全带外面还留着 BUBBLE_GAP=8 的余量，所以这几像素不会真被压到。
            slack = 8
            inside = (box[0] >= AVAIL.left() - slack and box[2] <= AVAIL.right() + slack
                      and box[1] >= AVAIL.top() - slack and box[3] <= AVAIL.bottom() + slack)
            check(f"{model_id} 落在安全带内（±{slack}px）", inside,
                  f"box={[round(v) for v in box]} avail="
                  f"[{AVAIL.left()},{AVAIL.top()},{AVAIL.right()},{AVAIL.bottom()}]")

            # 逼近程度：要么横向填满，要么纵向填满
            fill_w = (box[2] - box[0]) / AVAIL.width()
            fill_h = (box[3] - box[1]) / AVAIL.height()
            check(f"{model_id} 填满安全带（宽或高 ≥ 90%）",
                  max(fill_w, fill_h) >= 0.90,
                  f"fill_w={fill_w:.2f} fill_h={fill_h:.2f}")

            # 长宽比不能离谱地窄。
            #
            # 这条是补上的：之前的断言只看「落在安全带内 + 填满一个方向」，
            # 而漏掉了一个真 bug —— 换模型时 LoadModelJson / CreateRenderer
            # 跑在**没有当前 GL 上下文**的情况下，新模型的投影是坏的，
            # 自动取景只能让它纵向填满、横向却只剩应有的 1/3
            # （实测：直接加载 160x344，换过之后 63x346 = 长宽比 0.18）。
            # 看起来就是「模型变瘦了」，而上面两条断言全都会通过。
            #
            # 阈值取 0.30 而不是精确比对：模型一直在播待机动作，同一个模型不同
            # 瞬间的包围盒长宽比本身就在 0.40~0.53 之间晃（实测），
            # 精确比对会变成随机失败；0.30 能稳稳分开「正常」与「坏掉的 0.18」。
            aspect = (box[2] - box[0]) / max(1.0, box[3] - box[1])
            check(f"{model_id} 长宽比正常（>0.30；坏掉时约 0.18）", aspect > 0.30,
                  f"aspect={aspect:.3f}")

        # 水印：新模型有 Param137，默认应当被隐藏
        if model_id == "miku_v5":
            view.set_watermark_visible(True)
            pump(app, 0.3)
            view.grabFramebuffer().save(str(OUT_DIR / f"{model_id}_wm_on.png"))
            view.set_watermark_visible(False)
            pump(app, 0.3)
            check("水印参数可寻址", view.resolve_param("Param137") == "Param137")
        view.grabFramebuffer().save(str(OUT_DIR / f"{model_id}.png"))

    view.shutdown()
    view.close()
    pump(app, 0.3)
    app.quit()
    live2d.dispose()

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
