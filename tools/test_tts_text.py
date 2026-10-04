"""回归：TTS 前的文本清洗与分句必须与 PC **逐字一致**。

`backend/tts.py` 的 `normalize_text` 有 8 道正则、`split_sentences` 有切分+合并的
两段逻辑。手机端要移植一份，而移植错了**不会报错** —— 只会「念出来怪怪的」，
甚至整句变成静音（清洗后没有可用字符）。所以用同一套 golden 跨语言钉住：
本脚本比对 PC，`TtsTextTest.kt` 比对手机。

用法：
    .venv\\Scripts\\python.exe tools/test_tts_text.py            # 比对
    .venv\\Scripts\\python.exe tools/test_tts_text.py --update   # 重建 golden
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "backend"))

GOLDEN = BASE_DIR / "tools" / "tts_text_golden.json"

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILED.append(name)


#: 真实回复 + 各种会踩到清洗规则的边角
NORMALIZE_CASES = [
    "主人来啦！我等你好久了☆",
    "嘿嘿，能陪主人说话，Miku 好幸福呀♪",
    "太好啦！主人今天看起来心情不错呢(≧▽≦)",
    "唔…主人不要难过嘛，Miku 会一直陪着你的。",
    "（´▽｀）主人真可爱呢～",
    "[HAPPY] 开头标签应该已经被剥掉了，这里是兜底",
    "正文里也有 [HAPPY] 标签的情况",
    "【视觉】中文方括号标签",
    "（主人在微笑）括号里是中文，要保留",
    "こんにちは、ミクです",           # 假名会被白名单过滤 → 空
    "♪♪♪ ☆☆",                        # 全是装饰 → 空
    "……",                             # 只剩标点 → 空
    "嗯嗯，继续说吧，我在听呢～",
    "主人一定可以的！ミク 相信你哦！",
    "",                                # 空串
    "   ",                             # 只有空白
    "价格是 100 元，不要读错。",       # 数字与 ASCII 标点
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
]


def split_cases() -> list[dict]:
    cases = [
        "第一句。第二句！第三句？",
        "短。",
        "你好。",
        "一。二。三。四。",
        "这是一个特别特别特别特别特别特别特别特别特别长的句子，中间还有逗号，需要被切开。",
        "没有句末标点的结尾",
        "换行\n也算分隔",
        "主人来啦！我等你好久了☆ 嘿嘿，能陪主人说话，Miku 好幸福呀♪ 太好啦！",
        "",
        "。。。",
    ]
    out = []
    for text in cases:
        out.append({"input": text, "max_len": 40})
    # 换一个更小的 max_len，逼出「按逗号切」的分支
    out.append({
        "input": "这是一个很长的句子，中间有逗号，也有句号。第二句也不短，再来一点内容凑长度。",
        "max_len": 12,
    })
    return out


def speed_cases() -> list[dict]:
    """语速补偿的公式也要钉住 —— 手机端照搬了同一套实测标定表。

    基准速度取自 PC 的 `config.MINIMAX_SPEED`，并把它记进 golden，
    这样手机侧能用同一个基准算，比出来的才是同一个数。
    """
    import config

    base = config.MINIMAX_SPEED
    return [
        {"mm_emotion": mm, "text": text, "emotion": label, "base_speed": base}
        for mm, text, label in [
            ("neutral", "普通的一句话。", "NORMAL"),
            ("happy", "太好啦！", "HAPPY"),
            ("sad", "唔…有点难过。", "SAD"),
            ("angry", "哼！", "ANGRY"),
            ("surprised", "诶诶？！", "SURPRISED"),
            ("neutral", "这是一个很长的句子，超过三十个字，应该触发长句放慢的规则吧。", "NORMAL"),
            ("neutral", "", "NORMAL"),
        ]
    ]


def main() -> int:
    import config
    import tts

    update = "--update" in sys.argv

    norm = [{"input": c, "expect": tts.normalize_text(c)} for c in NORMALIZE_CASES]
    split = [
        {**c, "expect": tts.split_sentences(c["input"], c["max_len"])}
        for c in split_cases()
    ]
    speed = [
        {**c, "expect": tts.TextToSpeech._minimax_speed(
            c["mm_emotion"], c["text"], c["emotion"])}
        for c in speed_cases()
    ]

    if update or not GOLDEN.exists():
        GOLDEN.write_text(
            json.dumps(
                {"normalize": norm, "split": split, "speed": speed},
                ensure_ascii=False, indent=1,
            ),
            encoding="utf-8",
        )
        print(f"  已写入 golden：{GOLDEN.relative_to(BASE_DIR)}"
              f"（normalize {len(norm)} / split {len(split)} / speed {len(speed)}）")
        print("\n全部通过")
        return 0

    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    check("normalize 用例数一致", len(golden["normalize"]) == len(norm),
          f"golden {len(golden['normalize'])} / 现在 {len(norm)}")
    check("split 用例数一致", len(golden["split"]) == len(split),
          f"golden {len(golden['split'])} / 现在 {len(split)}")
    check("speed 用例数一致", len(golden["speed"]) == len(speed),
          f"golden {len(golden['speed'])} / 现在 {len(speed)}")

    diffs = 0
    for want, got in zip(golden["normalize"], norm):
        if want != got:
            diffs += 1
            if diffs <= 3:
                print(f"\n  ✗ normalize({want['input']!r})")
                print(f"    golden={want['expect']!r}")
                print(f"    现在  ={got['expect']!r}")
    check(f"{len(norm)} 个 normalize 用例一致", diffs == 0, f"有 {diffs} 个不同")

    diffs = 0
    for want, got in zip(golden["split"], split):
        if want != got:
            diffs += 1
            if diffs <= 3:
                print(f"\n  ✗ split({want['input']!r}, max={want['max_len']})")
                print(f"    golden={want['expect']!r}")
                print(f"    现在  ={got['expect']!r}")
    check(f"{len(split)} 个 split 用例一致", diffs == 0, f"有 {diffs} 个不同")

    # 语速补偿：手机的实现是**无条件**做反解的，所以只有 PC 开着这两个开关时
    # 才能比对；关掉的话 golden 记的是「不做反解」的结果，会误导。
    if not (config.MINIMAX_SPEED_NORMALIZE and config.MINIMAX_CONTENT_BIAS):
        check("PC 的 MINIMAX_SPEED_NORMALIZE / CONTENT_BIAS 都是开的"
              "（否则语速 golden 与手机端实现不同前提）", False,
              f"normalize={config.MINIMAX_SPEED_NORMALIZE} "
              f"content_bias={config.MINIMAX_CONTENT_BIAS}")
    else:
        diffs = 0
        for want, got in zip(golden["speed"], speed):
            if abs(want["expect"] - got["expect"]) > 1e-9:
                diffs += 1
                if diffs <= 3:
                    print(f"\n  ✗ speed({want['mm_emotion']}, {want['emotion']})"
                          f" golden={want['expect']} 现在={got['expect']}")
        check(f"{len(speed)} 个 speed 用例一致", diffs == 0, f"有 {diffs} 个不同")

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
