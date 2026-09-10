"""从下载的媒体里挑出一段干净的参考音频（给 GPT-SoVITS 用）。

GPT-SoVITS 的参考音频要求：
  - 5~10 秒、单人、干声、无伴奏无混响
  - 前后不要有明显空白或截断

做法：
  1. PyAV 解码成 32kHz 单声道 float32（不需要 ffmpeg 二进制）
  2. 按 20ms 帧算 RMS 包络
  3. 用「噪声底」判断有没有背景音乐：纯人声的字间静音会非常低，
     带 BGM 的底噪会被抬起来
  4. 找出连续语音段，挑能量稳定、长度合适的一段导出

用法:
    python tools/prepare_ref_audio.py <输入媒体> [--out out.wav] [--seconds 8]
    python tools/prepare_ref_audio.py <输入媒体> --analyze-only
"""
from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import av
import numpy as np

TARGET_SR = 32000
FRAME_MS = 20


def decode_mono(path: Path, target_sr: int = TARGET_SR) -> np.ndarray:
    """用 PyAV 解码成指定采样率的单声道 float32。"""
    chunks: list[np.ndarray] = []
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="flt", layout="mono", rate=target_sr)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        raise RuntimeError("解码失败，没有音频数据")
    return np.concatenate(chunks).astype(np.float32)


def rms_envelope(x: np.ndarray, sr: int, frame_ms: int = FRAME_MS) -> tuple[np.ndarray, int]:
    hop = max(1, int(sr * frame_ms / 1000))
    n = len(x) // hop
    trimmed = x[: n * hop].reshape(n, hop)
    env = np.sqrt(np.mean(trimmed.astype(np.float64) ** 2, axis=1) + 1e-12)
    return env, hop


def analyze(x: np.ndarray, sr: int) -> dict:
    env, hop = rms_envelope(x, sr)
    db = 20 * np.log10(env + 1e-9)
    floor = float(np.percentile(db, 10))
    peak = float(np.percentile(db, 95))
    speech_th = floor + (peak - floor) * 0.35
    voiced = db > speech_th
    silence_ratio = 1.0 - voiced.mean()

    # 连续的语音段
    segs: list[tuple[int, int]] = []
    i = 0
    while i < len(voiced):
        if voiced[i]:
            j = i
            while j < len(voiced) and voiced[j]:
                j += 1
            if (j - i) * hop / sr >= 1.0:      # 至少 1 秒
                segs.append((i, j))
            i = j
        else:
            i += 1
    return {
        "duration": len(x) / sr,
        "floor_db": floor,
        "peak_db": peak,
        "silence_ratio": silence_ratio,
        "segments": segs,
        "hop": hop,
        "db": db,
    }


def best_segment(info: dict, sr: int, want_sec: float) -> tuple[int, int]:
    """在语音段里挑能量最平稳、且长度足够的一段。"""
    hop = info["hop"]
    db = info["db"]
    want = int(want_sec * sr / hop)
    scored = []
    for a, b in info["segments"]:
        length = b - a
        if length < want:
            continue
        # 在段内滑动，找标准差最小（最平稳）的窗口
        best = None
        step = max(1, (length - want) // 12)
        for s in range(a, b - want + 1, step):
            w = db[s : s + want]
            score = float(np.std(w))
            if best is None or score < best[0]:
                best = (score, s, s + want)
        if best:
            scored.append((best[0], best[1], best[2], length))
    if not scored:
        # 没有足够长的段，就取最长的一段
        if not info["segments"]:
            return 0, min(len(db), want)
        a, b = max(info["segments"], key=lambda t: t[1] - t[0])
        end = min(b, a + want)
        return a, end
    scored.sort(key=lambda t: t[0])
    _, s, e, _ = scored[0]
    return s, e


def save_wav(path: Path, x: np.ndarray, sr: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 峰值归一化到 -3dBFS，避免削顶
    peak = float(np.max(np.abs(x))) or 1.0
    x = x / peak * 0.7
    pcm = np.clip(x * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(pcm.tobytes())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--analyze-only", action="store_true")
    args = ap.parse_args()

    for src in args.inputs:
        if not src.exists():
            print(f"[skip] not found: {src}")
            continue
        x = decode_mono(src)
        info = analyze(x, TARGET_SR)
        print(f"\n===== {src.name} =====")
        print(f"  时长        : {info['duration']:.1f}s")
        print(f"  噪声底      : {info['floor_db']:.1f} dB", end="")
        # 纯人声字间静音通常 <= -55dB；带 BGM 会被抬到 -40dB 以上
        if info["floor_db"] > -45:
            print("   <-- 偏高，可能有背景音乐")
        else:
            print("   (低，像纯人声)")
        print(f"  语音峰值    : {info['peak_db']:.1f} dB")
        print(f"  静音占比    : {info['silence_ratio']*100:.1f}%")
        print(f"  连续语音段  : {len(info['segments'])} 段")
        top = sorted(info["segments"], key=lambda t: t[1] - t[0], reverse=True)[:5]
        for a, b in top:
            print(f"     {a*info['hop']/TARGET_SR:7.2f}s ~ {b*info['hop']/TARGET_SR:7.2f}s"
                  f"  ({b*info['hop']/TARGET_SR - a*info['hop']/TARGET_SR:.1f}s)")

        if not args.analyze_only:
            a, b = best_segment(info, TARGET_SR, args.seconds)
            clip = x[a * info["hop"] : b * info["hop"]]
            out = args.out or src.with_name(src.stem + f"_ref{int(args.seconds)}s.wav")
            save_wav(out, clip, TARGET_SR)
            print(f"  -> 导出 {out.name}  ({len(clip)/TARGET_SR:.2f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
