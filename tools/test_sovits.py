"""测试 GSV-TTS-Lite（GPT-SoVITS）本地合成。

首次运行会自动下载预训练模型（走 hf-mirror）。
输出 .diag/sovits_out.wav 并打印耗时/实时率。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# 必须在 import gsv_tts 之前设置，否则 huggingface_hub 会缓存官方地址
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

OUT = BASE / ".diag"
OUT.mkdir(exist_ok=True)
REF = BASE / "assets" / "voice" / "miku_ref.wav"
# 参考音频的转写文本（faster-whisper 转出，zh 置信度 1.00）。
# infer() 的 prompt_audio_text 是必填的「风格参考文本」，必须与 prompt 音频内容一致。
REF_TEXT = "这就是为什么我要去商店修理它。"


def vram(tag: str) -> None:
    try:
        import torch

        if torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info()
            alloc = torch.cuda.memory_allocated()
            print(f"    [VRAM {tag}] 已用 {(total-free)/1e9:.2f}GB / 共 {total/1e9:.2f}GB"
                  f"  torch 分配 {alloc/1e9:.2f}GB  空闲 {free/1e9:.2f}GB", flush=True)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    ref = Path(args[0]) if len(args) > 0 else REF
    text = args[1] if len(args) > 1 else "主人你好呀，我是初音未来，今天也一起加油吧。"
    device = "cpu" if "--cpu" in flags else ("cuda" if "--cuda" in flags else None)
    use_bert = "--no-bert" not in flags

    print(f"参考音频: {ref}  (存在={ref.exists()})")
    print(f"合成文本: {text}")
    print(f"device={device or '(默认)'}  use_bert={use_bert}")
    print("HF_ENDPOINT =", os.environ.get("HF_ENDPOINT"))

    t0 = time.time()
    from gsv_tts import TTS

    print(f"[{time.time()-t0:6.1f}s] import gsv_tts 完成")

    # 默认模型目录是 ~/.cache/gsv，改到项目 data/ 下（也和运行时保持一致）
    models_dir = BASE / "data" / "gsv-models"
    models_dir.mkdir(parents=True, exist_ok=True)
    print("models_dir =", models_dir)

    tts = TTS(use_bert=use_bert, models_dir=str(models_dir), device=device)
    print(f"[{time.time()-t0:6.1f}s] TTS() 构造完成（bert={use_bert}, device={device or '默认'}）")
    vram('after TTS ctor')

    tts.load_gpt_model()
    print(f"[{time.time()-t0:6.1f}s] GPT 模型加载完成")
    vram('after GPT')

    tts.load_sovits_model()
    print(f"[{time.time()-t0:6.1f}s] SoVITS 模型加载完成")
    vram('after SoVITS')

    tts.cache_spk_audio(str(ref))
    print(f"[{time.time()-t0:6.1f}s] 参考音频已缓存")
    vram('after cache_spk')

    t_start = time.time()
    audio = tts.infer(
        spk_audio_path=str(ref),
        prompt_audio_path=str(ref),
        prompt_audio_text=REF_TEXT,
        text=text,
        text_language="zh",
        prompt_language="zh",
    )
    gen = time.time() - t_start

    out = OUT / "sovits_out.wav"
    audio.save(str(out))
    print(f"[{time.time()-t0:6.1f}s] 合成完成，耗时 {gen:.2f}s -> {out}")

    # 时长与实时率
    import wave

    try:
        with wave.open(str(out), "rb") as f:
            dur = f.getnframes() / float(f.getframerate() or 1)
            print(f"输出时长 {dur:.2f}s   RTF = {gen/max(dur,1e-6):.3f}")
    except Exception as e:  # noqa: BLE001
        print("读取输出时长失败:", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
