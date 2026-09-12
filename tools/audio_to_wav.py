"""把下载的 m4a 转成 44.1kHz 立体声 WAV，并报告音频特征。

用 PyAV（项目已有依赖）解码，避免依赖外部 ffmpeg.exe。
"""
from __future__ import annotations

import sys
from pathlib import Path

import av
import numpy as np
import soundfile as sf

SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".tmp/audio/miku_raw.m4a")
DST = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(".tmp/audio/miku_raw.wav")
TARGET_SR = 44100


def main() -> int:
    container = av.open(str(SRC))
    stream = container.streams.audio[0]
    resampler = av.AudioResampler(
        format="fltp", layout="stereo", rate=TARGET_SR
    )

    chunks: list[np.ndarray] = []
    for frame in container.decode(stream):
        for out in resampler.resample(frame):
            arr = out.to_ndarray()          # (channels, samples) float32
            chunks.append(arr)
    # flush
    for out in resampler.resample(None):
        chunks.append(out.to_ndarray())
    container.close()

    audio = np.concatenate(chunks, axis=1)   # (2, N)
    data = audio.T.copy()                    # (N, 2) for soundfile
    sf.write(str(DST), data, TARGET_SR, subtype="PCM_16")

    dur = data.shape[0] / TARGET_SR
    mono = data.mean(axis=1)
    rms = float(np.sqrt((mono ** 2).mean()))
    peak = float(np.abs(mono).max())
    # 估算「安静段」占比，用于判断有没有干净的间奏可切
    win = int(0.5 * TARGET_SR)
    n = len(mono) // win
    seg_rms = np.array([np.sqrt((mono[i * win:(i + 1) * win] ** 2).mean()) for i in range(n)])
    quiet = float((seg_rms < rms * 0.15).mean())

    print(f"  源文件     : {SRC.name}  ({SRC.stat().st_size/1024/1024:.2f} MB)")
    print(f"  输出       : {DST}  ({DST.stat().st_size/1024/1024:.2f} MB)")
    print(f"  采样率     : {TARGET_SR} Hz  声道: {data.shape[1]}")
    print(f"  时长       : {dur:.1f} 秒 ({dur/60:.1f} 分钟)")
    print(f"  整体 RMS   : {rms:.4f}   峰值: {peak:.3f}")
    print(f"  安静段占比 : {quiet*100:.1f}%  (0.5s 窗口内 RMS < 15% 均值的比例)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
