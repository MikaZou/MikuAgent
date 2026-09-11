"""验证 CPU 回退：GSV 在 CPU 上默认用 fp16，而 CPU 的 SDPA 不支持 fp16。

试 dtype=float32 能否修好。
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "backend"))
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

import config  # noqa: E402


def main() -> int:
    dtype = sys.argv[1] if len(sys.argv) > 1 else "float32"
    t0 = time.time()
    from gsv_tts import TTS

    print(f"[{time.time()-t0:6.1f}s] import ok, 试 device=cpu dtype={dtype}", flush=True)
    try:
        engine = TTS(
            models_dir=str(config.TTS_MODELS_DIR),
            device="cpu",
            dtype=dtype,
            use_bert=True,
        )
        print(f"[{time.time()-t0:6.1f}s] TTS() 构造完成", flush=True)
        engine.load_gpt_model()
        engine.load_sovits_model()
        print(f"[{time.time()-t0:6.1f}s] 模型加载完成", flush=True)
        ref = config.resolve_path(config.TTS_REF_AUDIO)
        engine.cache_spk_audio(str(ref))
        print(f"[{time.time()-t0:6.1f}s] 参考音频缓存完成", flush=True)
        t1 = time.time()
        audio = engine.infer(
            spk_audio_path=str(ref),
            prompt_audio_path=str(ref),
            prompt_audio_text=config.TTS_PROMPT_TEXT,
            text="你好呀，我是初音未来。",
            text_language="zh",
            prompt_language="zh",
        )
        gen = time.time() - t1
        out = BASE / "data" / "tts-cache" / "cpu_test.wav"
        audio.save(str(out))
        import wave

        with wave.open(str(out), "rb") as f:
            dur = f.getnframes() / float(f.getframerate() or 1)
        print(f">>> CPU 合成成功！{dur:.2f}s 音频 / 耗时 {gen:.2f}s (RTF {gen/max(dur,1e-6):.2f})", flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f">>> CPU dtype={dtype} 失败：{type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
