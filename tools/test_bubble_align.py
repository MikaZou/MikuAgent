"""验证「气泡下沿与模型始终对齐」。

需求背景：气泡高度随内容变化（72~188px）。如果从顶部往下长，短消息下方
就会留一大片空白；如果让模型跟着气泡跑，空白又会被推到窗口底部。
最终方案是**底部对齐**：气泡下沿钉死在 BUBBLE_TOP + BUBBLE_MAX_H，
内容少时向上收、内容多时向上长，模型位置完全不动。

这个工具用真实 PetWindow（需要 OpenGL）跑四种长度的消息，断言：
  * 气泡下沿恒定（不随消息长短漂移）
  * 气泡与模型顶部的间距恒定
  * 超长消息时气泡顶部不越过 BUBBLE_TOP（否则会盖住角标按钮）

用法：python tools/test_bubble_align.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

import config  # noqa: E402

SHORT = "好哦～"
MEDIUM = "主人好呀，我是初音未来，ミクです！♪ 最喜欢唱歌跳舞了。"
LONG = (
    "主人好呀，我是初音未来，ミクです！♪ 从歌声里诞生的16岁虚拟歌姬，"
    "最喜欢唱歌和跳舞了，还超爱吃草莓和葱——ね，葱真的超棒的！"
    "擅长的当然是创作旋律，听到喜欢的歌会忍不住哼起来（´▽｀）\n"
    "最喜欢陪在主人身边了，不管是聊天、玩游戏还是唱歌给你听，"
    "ミク都超级开心☆ 以后也让我一直陪着主人好不好？最喜欢主人了！(≧▽≦)"
)

failures: list[str] = []


def main() -> int:
    import live2d.v3 as live2d
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    live2d.init()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    from agent import MikuAgent
    from memory import MemoryStore
    from stt import SpeechToText
    from tts import TextToSpeech
    from ui import pet_window as pw
    from ui.pet_window import PetWindow

    memory = MemoryStore(config.DB_PATH)
    window = PetWindow(memory, MikuAgent(memory), SpeechToText(), TextToSpeech())
    window.start()

    def settle() -> None:
        for _ in range(8):
            app.processEvents()
            window.bubble.apply_content_height()
            window._layout_children()

    def report() -> None:
        model_top = window._model_top
        print(f"窗口 {window.width()}x{window.height()} | "
              f"BUBBLE_TOP={pw.BUBBLE_TOP} BUBBLE_MAX_H={pw.BUBBLE_MAX_H} | "
              f"模型顶部={model_top}")
        print(f"{'消息':6s} {'气泡高':>6s} {'气泡y':>6s} {'下沿':>6s} "
              f"{'距模型':>7s} {'按钮y':>6s} {'按钮距气泡':>10s}")
        print("-" * 62)

        bottoms: list[int] = []
        gaps: list[int] = []
        btn_gaps: list[int] = []
        for name, text in [
            ("短", SHORT),
            ("中", MEDIUM),
            ("长", LONG),
            ("超长", LONG * 2),
        ]:
            window.bubble.show_message(text, "HAPPY", typewriter=False)
            settle()
            b = window.bubble
            bottom = b.y() + b.height()
            gap = (model_top or 0) - bottom
            btn_y = window.btn_min.y()
            btn_gap = b.y() - (btn_y + pw.BUTTON_SIZE)
            bottoms.append(bottom)
            gaps.append(gap)
            btn_gaps.append(btn_gap)
            print(f"{name:6s} {b.height():6d} {b.y():6d} {bottom:6d} "
                  f"{gap:7d} {btn_y:6d} {btn_gap:10d}")

        span_bottom = max(bottoms) - min(bottoms)
        span_gap = max(gaps) - min(gaps)
        span_btn = max(btn_gaps) - min(btn_gaps)
        print()
        print(f"气泡下沿波动  : {span_bottom}px（应为 0）")
        print(f"距模型间距波动: {span_gap}px（应为 0）")
        # 气泡到顶端(max height)时按钮只能贴到 BUTTON_MARGIN，那时间距会更大，
        # 所以只要求「不为负（不重叠）」且「短消息时按钮跟着下移」
        print(f"按钮与气泡间距: {min(btn_gaps)}~{max(btn_gaps)}px")
        if span_bottom:
            failures.append(f"气泡下沿随消息长度漂移 {span_bottom}px，没有对齐")
        if span_gap:
            failures.append(f"气泡与模型间距漂移 {span_gap}px，没有对齐")
        if min(gaps) < 0:
            failures.append(f"气泡压到了模型上（最小间距 {min(gaps)}px）")
        if min(btn_gaps) < 0:
            failures.append(f"角标按钮压到了气泡上（间距 {min(btn_gaps)}px）")
        # 气泡上移时按钮必须跟着上移，否则又变成「钉死在顶端」
        if not (btn_gaps[0] > btn_gaps[-1]):
            failures.append(
                f"短消息({btn_gaps[0]}px)到超长消息({btn_gaps[-1]}px)按钮没有跟着气泡移动"
                "——按钮被钉死在窗口顶端了"
            )

        window.bubble.show_message(LONG * 2, "HAPPY", typewriter=False)
        settle()
        top = window.bubble.y()
        if top < pw.BUBBLE_TOP:
            failures.append(
                f"超长消息时气泡顶部到了 y={top}，越过 BUBBLE_TOP={pw.BUBBLE_TOP}，会盖住角标按钮"
            )
        else:
            print(f"超长消息气泡顶部 y={top} >= BUBBLE_TOP={pw.BUBBLE_TOP}  [OK]")

        print()
        if failures:
            for f in failures:
                print(f"  [X] {f}")
        else:
            print("气泡下沿与模型始终对齐 [OK]")
        app.quit()

    QTimer.singleShot(9000, report)
    QTimer.singleShot(45000, app.quit)
    app.exec()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
