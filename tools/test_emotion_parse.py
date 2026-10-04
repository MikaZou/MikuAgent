"""回归：情感标签解析必须与 PC **逐字一致**。

`parse_emotion` 只有二十来行，却已经咬过两次：

* 它返回 `(情感, 正文)`，而调用方按 `(正文, 情感)` 解构 —— 两个 String 的 `Pair`，
  编译器一句话都不会说。表现是气泡里显示「HAPPY」当正文、TTS 念出「HAPPY」。
* 模型偶尔**不带方括号**就把标签写在开头（实测「HAPPY 收到收到～…」），
  原来的规则只认方括号，于是标签留在正文里。

两端的实现是各写一份的，所以用同一套 golden 钉住。手机侧是 `EmotionParseTest.kt`。

用法：
    .venv\\Scripts\\python.exe tools/test_emotion_parse.py            # 比对
    .venv\\Scripts\\python.exe tools/test_emotion_parse.py --update   # 重建 golden
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "backend"))

GOLDEN = BASE_DIR / "tools" / "emotion_parse_golden.json"

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILED.append(name)


#: 覆盖三种标签位置、误伤风险、以及空/异常输入
CASES = [
    # —— 正常：开头带方括号 ——
    "[HAPPY] 主人来啦！我等你好久了☆",
    "[NORMAL] 原来如此呀。",
    "[sad] 小写也应该认出来",
    "[SURPRISED]哇没有空格",
    "  [MOTIVATED]   前面有空白",
    # —— 自由发挥：开头不带方括号（这次踩到的） ——
    "HAPPY 收到收到～PC 这条路走得通だよ！☆",
    "SAD 主人不要难过嘛",
    "NORMAL 嗯嗯，继续说。",
    # —— 正文中间 ——
    "…吗～？ [HAPPY] 当然可以呀！",
    "开头正常。中间又冒了一个 [ANGRY] 标签。",
    "只有中间 [EMPATHY] 这一处标签",
    # —— 不该误伤的 ——
    "HAPPY 这个词出现在正文里但我只是在说自己的心情，开头没有标签",
    "HAPPYS 这种更长的单词不该被切开",
    "happy 小写的裸标签不认（只认全大写，避免误吞正文）",
    "主人说 [1] 和 [2] 这种数字方括号不该被动",
    "[UNKNOWN] 不认识的标签保留原样，不当情感",
    # —— 空与异常 ——
    "",
    "   ",
    "没有标签的普通回复。",
    "[HAPPY]",
    "[HAPPY]   ",
    "NORMAL ",
]


def main() -> int:
    import agent

    update = "--update" in sys.argv
    rendered = [
        {"input": c, "emotion": agent.parse_emotion(c)[0], "reply": agent.parse_emotion(c)[1]}
        for c in CASES
    ]

    if update or not GOLDEN.exists():
        GOLDEN.write_text(
            json.dumps({"cases": rendered}, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"  已写入 golden：{GOLDEN.relative_to(BASE_DIR)}（{len(rendered)} 个用例）")
        print("\n全部通过")
        return 0

    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]
    check("用例数一致", len(golden) == len(rendered),
          f"golden {len(golden)} / 现在 {len(rendered)}")

    diffs = 0
    for want, got in zip(golden, rendered):
        if want != got:
            diffs += 1
            if diffs <= 4:
                print(f"\n  ✗ {want['input']!r}")
                print(f"    golden  emotion={want['emotion']!r} reply={want['reply']!r}")
                print(f"    现在    emotion={got['emotion']!r} reply={got['reply']!r}")
    check(f"{len(golden)} 个用例逐字一致", diffs == 0, f"有 {diffs} 个不同")

    # 单独强调这次踩到的那条：裸标签必须被当成情感、且不出现在正文里
    bare = agent.parse_emotion("HAPPY 收到收到～PC 这条路走得通だよ！☆")
    check("裸标签被识别为情感", bare[0] == "HAPPY", str(bare))
    check("裸标签不再留在正文里", bare[1].startswith("收到收到"), bare[1])

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
