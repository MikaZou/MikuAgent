"""导出候选参考音频段落并播放，用于人工判断分离质量。

用法::

    python tools/export_vocal_segment.py p2 A_chorus --play
    python tools/export_vocal_segment.py p1 B_with_break

第一个参数是来源：p1（现场版）/ p2（手书版）
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

BASE = Path(__file__).resolve().parent.parent
AUD = BASE / ".tmp" / "audio"

SOURCES = {
    "p1": AUD / "sep" / "htdemucs" / "miku_raw",
    "p2": AUD / "sep2" / "htdemucs" / "miku_p2",
    "p2ft": AUD / "sep3" / "htdemucs_ft" / "miku_p2",
    # V4C 发布会打招呼（说话，官方来源，分离极干净）
    "v4c": AUD / "sep4" / "htdemucs" / "miku_v4c",
}

# 人声能量较高的段落（据 compare_separation 的包络分布挑选）
SEGMENTS = {
    "A_chorus": (300.0, 318.0),
    "B_with_break": (148.0, 168.0),
    "C_verse": (240.0, 258.0),
    "D_late": (330.0, 350.0),
    # V4C：导入前的静音到结束，去掉首尾空白
    "speech_full": (5.5, 62.0),
    "speech_30s": (6.0, 36.0),
    "speech_15s": (10.0, 25.0),
}


def main() -> int:
    src_key = sys.argv[1] if len(sys.argv) > 1 else "p2"
    which = sys.argv[2] if len(sys.argv) > 2 else "A_chorus"
    play = "--play" in sys.argv

    folder = SOURCES.get(src_key)
    if folder is None or not (folder / "vocals.wav").exists():
        print(f"找不到 {src_key} 的分离结果：{folder}")
        return 1

    voc, sr = sf.read(str(folder / "vocals.wav"), always_2d=True)
    nov, _ = sf.read(str(folder / "no_vocals.wav"), always_2d=True)

    start, end = SEGMENTS.get(which, SEGMENTS["A_chorus"])
    end = min(end, len(voc) / sr)
    i, j = int(start * sr), int(end * sr)
    seg, seg_ac = voc[i:j], nov[i:j]

    out_dir = AUD / "candidates"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{src_key}_{which}.wav"
    sf.write(str(path), seg, sr, subtype="PCM_16")

    rms = float(np.sqrt((seg.mean(axis=1) ** 2).mean()))
    peak = float(np.abs(seg).max())
    print(f"  来源     : {src_key}   片段: {which}  {start:.0f}s ~ {end:.0f}s ({end-start:.0f} 秒)")
    print(f"  已导出   : {path.relative_to(BASE)}  ({path.stat().st_size/1024:.0f} KB)")
    print(f"  人声 RMS : {20*np.log10(max(rms,1e-9)):.1f} dBFS   峰值: {peak:.3f}")

    if play:
        import sounddevice as sd
        dur = end - start
        print(f"  ▶ 播放【人声轨】{dur:.0f} 秒 …")
        sd.play(seg[:, :2], sr)
        time.sleep(dur + 0.5)
        sd.stop()
        time.sleep(0.8)
        print("  ▶ 播放【同一段的伴奏轨】用于对比残留量 …")
        sd.play(seg_ac[:, :2], sr)
        time.sleep(dur + 0.5)
        sd.stop()
        print("  播放结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
