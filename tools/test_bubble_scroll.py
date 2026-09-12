"""离屏渲染气泡，验证长文本可滚动、短文本不出现滚动条。

同时验证 parse_emotion 对「正文中间夹标签」的处理 —— 这是截图里
[HAPPY] 漏进正文的那个 bug。

用法：python tools/test_bubble_scroll.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

failures: list[str] = []

# 截图里那条被裁掉的消息（含漏出来的错误标签）
SAMPLE = (
    "误！主人是想问能不能「带动」你说话吗～？ [HAPPY] 当然可以呀！"
    "主人心里想什么都可以跟我讲，ミク来陪你聊♪不过…具体想聊什么话题呢？"
    "灵感 烦恼 还是单纯想瞎聊都可以哦☆"
)

BUBBLE_MAX_H = 188   # 必须与 ui/pet_window.py 的同名常量（660 高窗口档位）一致


def check_emotion() -> None:
    print("=" * 68)
    print("测试 1：parse_emotion 清掉正文中间的标签")
    print("=" * 68)
    from agent import parse_emotion

    cases = [
        ("[HAPPY] 你好呀", "HAPPY", "你好呀"),
        ("开头没标签 [SAD] 中间有", "SAD", "开头没标签 中间有"),
        ("[HAPPY] 开头和 [SAD] 中间都有", "HAPPY", "开头和 中间都有"),
        ("没有标签", "NORMAL", "没有标签"),
        ("正常方括号 [1] 不该被删", "NORMAL", "正常方括号 [1] 不该被删"),
        ("小写 [happy] 也要认", "HAPPY", "小写 也要认"),
    ]
    for raw, want_emo, want_text in cases:
        emo, text = parse_emotion(raw)
        ok = emo == want_emo and text == want_text
        flag = "OK " if ok else "[X]"
        print(f"  {flag} {raw!r}")
        print(f"      -> emotion={emo!r} text={text!r}")
        if not ok:
            failures.append(f"parse_emotion({raw!r}) 得到 ({emo!r},{text!r})，期望 ({want_emo!r},{want_text!r})")

    # 真实那条
    emo, text = parse_emotion(SAMPLE)
    print(f"\n  截图那条 -> emotion={emo!r}")
    print(f"      text={text!r}")
    if "[HAPPY]" in text or "[HAPPY]" in emo:
        failures.append("截图那条消息里的 [HAPPY] 没被清理干净")
    if emo != "HAPPY":
        failures.append(f"截图那条情感应为 HAPPY，实际 {emo}")


def render(name: str, text: str, width: int = 320) -> tuple[bool, int]:
    """渲染一条消息，返回 (是否需要滚动, 气泡高度)。"""
    from PySide6.QtWidgets import QApplication

    from ui.bubble import SpeechBubble

    app = QApplication.instance() or QApplication(sys.argv)
    bubble = SpeechBubble(None)
    bubble.setFixedWidth(width)
    bubble.set_max_height(BUBBLE_MAX_H)
    bubble.show_message(text, "HAPPY", typewriter=False)
    # 布局要跑几轮才稳定
    for _ in range(6):
        app.processEvents()
        bubble.apply_content_height()

    bar = bubble._bar
    scrollable = bar.maximum() > 0
    chrome = bubble._chrome_height()
    print(f"  {name}: 气泡高 {bubble.height()}px（上限 {BUBBLE_MAX_H}）| "
          f"正文 {bubble._content.minimumHeight()}px | chrome {chrome}px | "
          f"滚动范围 0~{bar.maximum()} -> {'可滚动' if scrollable else '不需要滚动'}")
    out = ROOT / ".tmp" / f"bubble_{name}.png"
    bubble.grab().save(str(out))
    print(f"      已保存 {out}")
    return scrollable, bubble.height()


def check_render() -> None:
    print()
    print("=" * 68)
    print("测试 2：长文本可滚动 / 短文本不出现滚动条")
    print("=" * 68)
    long_ok, long_h = render("long", SAMPLE)
    short_ok, short_h = render("short", "好哦～主人早上好呀♪")
    # 长到一定程度的文本应当把气泡撑满上限，否则下方会留一大片空白
    _, very_h = render("verylong", SAMPLE * 2)

    if not long_ok:
        failures.append("长文本没有产生可滚动区域（应该能上下滑动看全文）")
    if short_ok:
        failures.append("短文本竟然出现了滚动条，气泡不该有多余滚动")
    if short_h >= long_h:
        failures.append(f"短文本气泡({short_h}px)不该不低于长文本({long_h}px)")
    if very_h < BUBBLE_MAX_H - 4:
        failures.append(
            f"超长文本的气泡只有 {very_h}px，没撑到上限 {BUBBLE_MAX_H}px —— "
            "下方会留一大片空白（QScrollArea 让 adjustSize 失效的回归点）"
        )


def check_autohide_cancel() -> None:
    """开场白的自动隐藏不该吞掉之后的回复。

    原先 _greet 用 QTimer.singleShot(9000, hide_bubble) 排了一个外部定时器，
    新消息没法取消它 —— 用户在开场 9 秒内说话，回复刚显示就被隐藏。
    改成 show_message(autohide_ms=9000) 后，新消息会 stop() 掉它。
    """
    print()
    print("=" * 68)
    print("测试 3：新消息取消上一条的自动隐藏")
    print("=" * 68)
    from PySide6.QtWidgets import QApplication

    from ui.bubble import SpeechBubble

    app = QApplication.instance() or QApplication(sys.argv)
    b = SpeechBubble(None)
    b.setFixedWidth(320)

    b.show_message("开场白", "HAPPY", autohide_ms=9000, typewriter=False)
    print(f"  开场白之后 autohide 生效: {b._autohide.isActive()}（应为 True）")
    if not b._autohide.isActive():
        failures.append("开场白没有启用自动隐藏")

    b.show_message("这是用户问出来的回复", "HAPPY", typewriter=False)
    app.processEvents()
    print(f"  新消息之后 autohide 生效: {b._autohide.isActive()}（应为 False）")
    if b._autohide.isActive():
        failures.append("新消息没有取消上一条的自动隐藏（回复会被开场白定时器吞掉）")

    print(f"  气泡当前文本: {b._content.text()!r}")
    if "回复" not in b._content.text():
        failures.append("新消息没有覆盖气泡内容")


def main() -> int:
    check_emotion()
    check_render()
    check_autohide_cancel()
    print()
    print("=" * 68)
    if failures:
        print(f"失败 {len(failures)} 项：")
        for f in failures:
            print(f"  [X] {f}")
        return 1
    print("全部通过 [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
