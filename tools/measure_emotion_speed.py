"""实测 MiniMax 各 emotion 的实际语速，标定 / 复核 MINIMAX_EMOTION_RATE。

背景：同一个 voice_setting.speed 下，MiniMax 会因为 emotion 不同而改变真实
说话快慢 —— surprised/happy 比 neutral 快约 25~29%。用户听到的就是
「有时快有时慢」。

指标说明：只统计「有声时长」而不是文件总时长。否则某个情绪多带一点首尾
静音就会被误判成语速变慢（实测各情绪静音长度都在 0.4~0.7s，差异不大，
但用总时长会把这点噪声算进语速）。

注意：本工具直接调 _synth_minimax 绕过缓存。走 synthesize() 的话，同一
文本 + 同一情绪会命中缓存，重复采样拿到的是同一个文件，标准差恒为 0。

用法：
    python tools/measure_emotion_speed.py                # 当前配置（补偿生效）
    python tools/measure_emotion_speed.py --raw          # 关掉补偿，看原始倍率
    python tools/measure_emotion_speed.py -n 5 --delay 2 # 更多采样、放慢请求
    python tools/measure_emotion_speed.py --only sad,neutral,surprised
"""
from __future__ import annotations

import argparse
import os
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

TEXT = "今天天气不错，我们一起出去走走吧。"

# MiniMax emotion 取值 → 我们这边的 emotion 标签
LABEL_OF = {
    "neutral": "NORMAL",
    "happy": "HAPPY",
    "sad": "SAD",
    "angry": "ANGRY",
    "surprised": "SURPRISED",
}


def voiced_seconds(path: Path, thresh_db: float = -40.0) -> float:
    """返回有声段时长（秒）。按 10ms 帧 RMS 判定。"""
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if data.size == 0:
        return 0.0

    win = max(1, int(rate * 0.01))
    usable = data.size // win * win
    if usable == 0:
        return 0.0
    rms = np.sqrt((data[:usable].reshape(-1, win) ** 2).mean(axis=1))
    db = 20.0 * np.log10(np.maximum(rms, 1e-9))
    idx = np.where(db > thresh_db)[0]
    if idx.size == 0:
        return 0.0
    start = int(idx[0]) * win
    end = min(data.size, (int(idx[-1]) + 1) * win)
    return (end - start) / float(rate)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--samples", type=int, default=3, help="每个情绪的采样次数")
    ap.add_argument("--raw", action="store_true", help="强制关闭语速补偿，测原始倍率")
    ap.add_argument("--delay", type=float, default=1.2, help="每次请求间隔秒数，避开 RPM 限流")
    ap.add_argument("--only", default="", help="只测指定情绪，逗号分隔，如 sad,neutral")
    args = ap.parse_args()

    if args.raw:
        os.environ["MINIMAX_SPEED_NORMALIZE"] = "false"

    import importlib
    import tempfile
    import time

    import config
    import tts as T

    importlib.reload(config)
    importlib.reload(T)

    if config.TTS_ENGINE != "minimax":
        print(f"当前 TTS_ENGINE={config.TTS_ENGINE}，本工具只测 minimax。")
        return 2

    targets = dict(LABEL_OF)
    if args.only:
        wanted = [s.strip() for s in args.only.split(",") if s.strip()]
        unknown = [w for w in wanted if w not in LABEL_OF]
        if unknown:
            print(f"未知 emotion: {unknown}，可选 {list(LABEL_OF)}")
            return 2
        targets = {k: v for k, v in LABEL_OF.items() if k in wanted}

    engine = T.TextToSpeech()
    text = T.normalize_text(TEXT)
    n = len(text)
    mode = "原始（补偿关闭）" if args.raw else "补偿后"
    print(
        f"文本 {n} 字 | MINIMAX_SPEED={config.MINIMAX_SPEED} | {mode} | "
        f"每情绪 {args.samples} 次 | 间隔 {args.delay}s"
    )
    print()

    header = (
        f"{'emotion':11s} {'speed':>6s} {'有声均值s':>9s} {'标准差s':>8s} "
        f"{'变异系数':>8s} {'有声字/秒':>10s}"
    )
    print(header)
    print("-" * len(header))

    results: dict[str, float] = {}
    samples: dict[str, list[float]] = {mm: [] for mm in targets}
    tmpdir = Path(tempfile.mkdtemp(prefix="mm_speed_"))

    # 交叉轮询采样：分块顺序采样会把服务端的时间漂移误算成情绪差异
    for _ in range(args.samples):
        for mm, label in targets.items():
            out = tmpdir / f"{mm}_{time.time_ns()}.wav"
            try:
                got = engine._synth_minimax(text, label, out)
            except Exception as exc:  # noqa: BLE001
                print(f"  {mm} 合成失败: {exc}")
            else:
                if got and Path(got).exists():
                    samples[mm].append(voiced_seconds(Path(got)))
            if args.delay > 0:
                time.sleep(args.delay)

    for mm in targets:
        values = samples[mm]
        if not values:
            print(f"{mm:11s}  采样失败")
            continue
        mv = float(np.mean(values))
        sd = float(np.std(values))
        results[mm] = mv
        spd = engine._minimax_speed(mm, text, targets[mm])
        print(
            f"{mm:11s} {spd:6.2f} {mv:9.2f} {sd:8.2f} "
            f"{sd / mv:8.1%} {n / mv:10.2f}"
        )

    engine.shutdown()

    if not results:
        return 1

    rates = {mm: n / mv for mm, mv in results.items()}
    base = rates.get("neutral", float(np.mean(list(rates.values()))))
    print()
    print(f"以 neutral={base:.2f} 有声字/秒 为基准：")
    print(f"{'emotion':11s} {'实测倍率':>8s} {'期望倍率':>8s} {'偏差':>8s} {'判定':>6s}")
    for mm in targets:
        if mm not in rates:
            continue
        measured = rates[mm] / base
        want = T.EMOTION_SPEED_TARGET.get(targets[mm], 1.0)
        dev = measured / want
        # 单次噪声约 5~9%，偏差落在 ±15% 内就算命中
        mark = "OK" if abs(dev - 1.0) <= 0.15 else ("偏快" if dev > 1 else "偏慢")
        print(f"{mm:11s} {measured:8.3f} {want:8.3f} {dev:8.1%} {mark:>6s}")

    lo, hi = min(rates.values()), max(rates.values())
    want_lo = min(T.EMOTION_SPEED_TARGET.get(targets[m], 1.0) for m in rates)
    want_hi = max(T.EMOTION_SPEED_TARGET.get(targets[m], 1.0) for m in rates)
    print()
    print(
        f"实测离散度：{lo:.2f} ~ {hi:.2f} 字/秒（{hi / lo:.2f} 倍）  |  "
        f"设计离散度：{want_hi / want_lo:.2f} 倍"
    )
    if args.raw:
        print("（原始模式：MiniMax 未受控的情绪语速差异，用于更新 MINIMAX_EMOTION_RATE）")
    else:
        print("提示：MiniMax 同一请求本身有 5~9% 随机波动，单次采样偏差在 ±15% 内属正常。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
