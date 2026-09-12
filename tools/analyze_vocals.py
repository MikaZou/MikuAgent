"""分析 demucs 分离出的人声质量，并挑出一段适合音色克隆的参考音频。

判断依据（针对 MiniMax 音色复刻「10 秒 ~ 5 分钟干净人声」的要求）：
  * 底噪：伴奏段的人声轨应当接近静音。底噪越低，说明分离越干净。
  * 人声占比：整首歌里人声实际存在的比例。
  * 目标片段：连续 ≥12 秒、能量稳定、无长停顿的演唱段。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import soundfile as sf

BASE = Path(__file__).resolve().parent.parent
VOCALS = BASE / ".tmp" / "audio" / "sep" / "htdemucs" / "miku_raw" / "vocals.wav"
MIX = BASE / ".tmp" / "audio" / "miku_raw.wav"
OUT_DIR = BASE / ".tmp" / "audio"

WIN = 0.25          # 包络窗口（秒）
TARGET_LEN = 14.0   # 目标片段长度


def envelope(mono: np.ndarray, sr: int, win: float = WIN) -> np.ndarray:
    w = max(1, int(win * sr))
    n = len(mono) // w
    return np.sqrt((mono[: n * w].reshape(n, w) ** 2).mean(axis=1))


def db(x: float) -> float:
    return 20 * np.log10(max(x, 1e-9))


def main() -> int:
    if not VOCALS.exists():
        print("找不到 vocals.wav，请先跑 demucs")
        return 1

    voc, sr = sf.read(str(VOCALS), always_2d=True)
    mix, _ = sf.read(str(MIX), always_2d=True)
    voc_m = voc.mean(axis=1)
    mix_m = mix.mean(axis=1)
    dur = len(voc_m) / sr

    env_v = envelope(voc_m, sr)
    env_m = envelope(mix_m, sr)
    rms_v, rms_m = float(np.sqrt((voc_m ** 2).mean())), float(np.sqrt((mix_m ** 2).mean()))

    print("=" * 68)
    print("分离质量分析")
    print("=" * 68)
    print(f"  时长          : {dur:.1f} 秒")
    print(f"  原混音 RMS    : {rms_m:.4f}  ({db(rms_m):6.1f} dBFS)")
    print(f"  人声轨 RMS    : {rms_v:.4f}  ({db(rms_v):6.1f} dBFS)")
    print(f"  人声/混音能量 : {db(rms_v) - db(rms_m):+.1f} dB")

    # 底噪：取人声轨最安静的 20% 窗口
    quiet = np.percentile(env_v, 20)
    loud = np.percentile(env_v, 90)
    print(f"  人声轨底噪    : {db(quiet):6.1f} dBFS  (最安静 20% 窗口)")
    print(f"  人声轨峰值段  : {db(loud):6.1f} dBFS  (最响 10% 窗口)")
    print(f"  动态范围      : {db(loud) - db(quiet):.1f} dB  （越大说明人声与静默区分越明显）")

    # 有能量的人声窗口占比
    thresh = max(quiet * 4, rms_v * 0.25)
    active = env_v > thresh
    print(f"  人声存在占比  : {active.mean()*100:.1f}%")

    # 找最佳片段：在滑动窗口里选「平均能量高 + 波动小」的
    win_n = int(TARGET_LEN / WIN)
    best, best_score, best_i = None, -1e9, -1
    for i in range(0, len(env_v) - win_n):
        seg = env_v[i:i + win_n]
        if (seg < thresh).mean() > 0.15:      # 停顿太多就跳过
            continue
        score = seg.mean() - seg.std() * 0.5  # 能量高且稳定
        if score > best_score:
            best_score, best_i, best = score, i, seg
    if best is None:
        print("\n  ⚠️ 没找到连续 14 秒的稳定人声段")
        return 1

    start = best_i * WIN
    end = start + TARGET_LEN
    s_idx, e_idx = int(start * sr), int(end * sr)
    print()
    print(f"  最佳片段      : {start:.1f}s ~ {end:.1f}s  ({TARGET_LEN:.0f} 秒)")
    print(f"    片段 RMS    : {db(float(np.sqrt((voc_m[s_idx:e_idx] ** 2).mean()))):.1f} dBFS")
    print(f"    片段波动    : {best.std():.4f}（越小越平稳）")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    seg_path = OUT_DIR / "miku_vocals_14s.wav"
    sf.write(str(seg_path), voc[s_idx:e_idx], sr, subtype="PCM_16")
    full_path = OUT_DIR / "miku_vocals_full.wav"
    sf.write(str(full_path), voc, sr, subtype="PCM_16")
    print()
    print(f"  已导出片段    : {seg_path.relative_to(BASE)}")
    print(f"  已导出全曲人声: {full_path.relative_to(BASE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
