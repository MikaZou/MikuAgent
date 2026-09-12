"""从分离出的 v4c 说话人声里，切出一段适合做音色参考的音频并取得逐字文本。

GPT-SoVITS 的 `prompt_audio_text` 必须与参考音频**逐字一致**，否则音色会劣化，
所以这里用 faster-whisper 转写并输出带时间戳的分段，方便人工核对。

MiniMax 音色复刻另有要求：主参考 10 秒 ~ 5 分钟，示例音频 ≤8 秒。
本脚本会一并导出这两份。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import soundfile as sf

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "backend"))

import config  # noqa: E402

SRC = BASE / ".tmp" / "audio" / "sep4" / "htdemucs" / "miku_v4c" / "vocals.wav"
OUT = BASE / "assets" / "voice" / "miku_v4c"
SR = 32000          # GPT-SoVITS 用 32k；MiniMax 不挑采样率
SPEECH_START = 5.5
SPEECH_END = 62.0


def db(x: float) -> float:
    return 20 * np.log10(max(float(x), 1e-9))


def main() -> int:
    if not SRC.exists():
        print(f"找不到 {SRC}")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)

    audio, sr = sf.read(str(SRC), always_2d=True)
    mono = audio.mean(axis=1)

    # 去掉首尾空白，只保留说话段
    i, j = int(SPEECH_START * sr), int(min(SPEECH_END, len(mono) / sr) * sr)
    speech = mono[i:j]
    print(f"  源        : {SRC.name}  {len(mono)/sr:.1f}s @ {sr}Hz")
    print(f"  说话段    : {SPEECH_START:.1f}s ~ {SPEECH_END:.1f}s  ({len(speech)/sr:.1f}s)")
    print(f"  电平      : RMS {db(np.sqrt((speech**2).mean())):.1f} dBFS  "
          f"峰值 {np.abs(speech).max():.3f}")

    # 归一化到 -3 dBFS 峰值，留足余量又不要太小声
    target_peak = 10 ** (-3 / 20)
    gain = target_peak / max(np.abs(speech).max(), 1e-9)
    norm = (speech * gain).astype(np.float32)
    print(f"  归一化    : +{20*np.log10(gain):.1f} dB → 峰值 {np.abs(norm).max():.3f}")

    # 重采样到 32k（GPT-SoVITS 要求）
    if sr != SR:
        import av

        resampler = av.AudioResampler(format="fltp", layout="mono", rate=SR)
        # from_ndarray 必须显式给 layout，否则默认按立体声校验形状
        frame = av.AudioFrame.from_ndarray(
            norm.reshape(1, -1).astype(np.float32), format="flt", layout="mono"
        )
        frame.sample_rate = sr
        frames = [out.to_ndarray().reshape(-1) for out in resampler.resample(frame)]
        frames += [out.to_ndarray().reshape(-1) for out in resampler.resample(None)]
        norm32 = np.concatenate(frames) if frames else norm
    else:
        norm32 = norm

    full = OUT / "miku_v4c_speech_full.wav"
    sf.write(str(full), norm32, SR, subtype="PCM_16")
    print(f"\n  已导出主参考(56s) : {full.relative_to(BASE)}  ({full.stat().st_size/1024:.0f} KB)")

    # 示例音频 ≤8 秒
    ex = OUT / "miku_v4c_example_7s.wav"
    sf.write(str(ex), norm32[: int(7 * SR)], SR, subtype="PCM_16")
    print(f"  已展示例音频(7s)  : {ex.relative_to(BASE)}  ({ex.stat().st_size/1024:.0f} KB)")

    # ---- GPT-SoVITS 的短参考 ----
    # 注意：下面的时间戳是相对**已裁剪的** norm32（即 speech_full.wav）。
    # 词级时间戳本来就是转写 speech_full.wav 得到的，所以不要再减 SPEECH_START，
    # 否则会算出负索引、切出 0 字节的空文件（这个 bug 已踩过）。
    # 词级时间戳显示「大家好，我是初音未来。」落在 1.22s ~ 6.28s，
    # 这句被官方视频简介逐字确认（发布会现场原话），可放心当 prompt_audio_text。
    REF_TEXT = "大家好，我是初音未来。"
    ref_a, ref_b = 1.10, 6.50
    seg = norm32[int(ref_a * SR): int(ref_b * SR)]
    if len(seg) < SR:      # 切片异常时直接报错，别默默产出空文件
        raise RuntimeError(f"参考片段切片异常：{len(seg)} 采样点（{len(seg)/SR:.2f}s）")
    ref = OUT / "miku_v4c_ref_5s.wav"
    sf.write(str(ref), seg, SR, subtype="PCM_16")
    (OUT / "miku_v4c_ref_5s.txt").write_text(REF_TEXT, encoding="utf-8")
    print(f"  已导出短参考({ref_b-ref_a:.1f}s) : {ref.relative_to(BASE)}  "
          f"({ref.stat().st_size/1024:.0f} KB)")
    print(f"     对应文本 : {REF_TEXT}")

    # ---- 转写（用于 GPT-SoVITS 的 prompt_audio_text）----
    print("\n  正在转写（faster-whisper）…")
    from faster_whisper import WhisperModel

    model = WhisperModel(config.STT_MODEL, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        str(full), language="zh", beam_size=5, word_timestamps=True
    )
    print(f"  语言={info.language} 概率={info.language_probability:.2f}\n")
    print("  %8s %8s  %s" % ("起(s)", "止(s)", "文本"))
    print("  " + "-" * 60)
    rows = []
    for s in segments:
        print("  %8.2f %8.2f  %s" % (s.start, s.end, s.text.strip()))
        rows.append((s.start, s.end, s.text.strip()))

    plain = "".join(t for _, _, t in rows)
    print(f"\n  全文（{len(plain)} 字）：\n    {plain}")

    txt = OUT / "miku_v4c_speech_full.txt"
    txt.write_text(plain, encoding="utf-8")
    print(f"\n  已写文本  : {txt.relative_to(BASE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
