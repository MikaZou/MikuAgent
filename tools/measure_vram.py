"""TTS 显存/内存拆解：逐个组件加载并记录增量。

⚠️ 安全约束（上一版把系统内存吃干过，导致 NVIDIA 驱动失联）：
  - 默认**不做 infer**（infer 会额外加载 BERT + hubert + 说话人验证，是内存峰值）
  - 每步前检查可用内存，低于阈值立即中止
  - 一次只跑一个配置，避免多份模型同时驻留

用法:
    python tools/measure_vram.py base        # BERT 开（当前生产配置），只加载不推理
    python tools/measure_vram.py nobert      # BERT 关
    python tools/measure_vram.py base infer  # 额外跑一次推理（内存需求最高）
"""
from __future__ import annotations

import gc
import os
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "backend"))
os.environ.setdefault("HF_HOME", str(BASE / "data" / "hf-cache"))
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

import config  # noqa: E402

MIN_FREE_MB = 2200          # 可用内存低于这个值就中止，别把系统拖死


def free_mb() -> float:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Counter '\\Memory\\Available MBytes').CounterSamples[0].CookedValue"],
            capture_output=True, text=True, timeout=20,
        )
        return float(out.stdout.strip())
    except Exception:  # noqa: BLE001
        return -1.0


def gpu_used_mb() -> str:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=20,
        )
        return out.stdout.strip()
    except Exception:  # noqa: BLE001
        return "?"


def guard(where: str) -> None:
    f = free_mb()
    g = gpu_used_mb()
    print(f"  [守卫] {where}: 系统可用内存 {f:.0f} MB, GPU 占用 {g}", flush=True)
    if 0 < f < MIN_FREE_MB:
        print(f"  [中止] 可用内存不足 {MIN_FREE_MB} MB，停止以免拖垮系统", flush=True)
        sys.exit(3)


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "base"
    do_infer = len(sys.argv) > 2 and sys.argv[2] == "infer"

    import torch
    from gsv_tts import TTS

    kwargs = {
        "base": dict(use_bert=True),
        "nobert": dict(use_bert=False),
        "smallcache": dict(use_bert=False, gpt_cache=[(1, 512)], sovits_cache=[50]),
    }.get(which)
    if kwargs is None:
        print(f"未知配置 {which}")
        return 2

    print(f"配置 {which}: {kwargs}   推理={'是' if do_infer else '否'}")
    guard("起始")

    t0 = time.time()

    def step(label: str) -> None:
        a = torch.cuda.memory_allocated() / 1024**3
        r = torch.cuda.memory_reserved() / 1024**3
        print(f"  {label:<32} 显存 allocated {a:5.2f} GB / reserved {r:5.2f} GB"
              f"   系统可用 {free_mb():6.0f} MB   +{time.time()-t0:4.1f}s", flush=True)

    engine = TTS(models_dir=str(config.TTS_MODELS_DIR), device="cuda",
                 dtype="float16", **kwargs)
    step("TTS() 构造")

    engine.load_gpt_model()
    step("+ GPT 主模型")

    engine.load_sovits_model()
    step("+ SoVITS 声码器")

    ref = config.resolve_path(config.TTS_REF_AUDIO)
    engine.cache_spk_audio(str(ref))
    step("+ 音色参考缓存")

    if do_infer:
        guard("infer 前")
        engine.infer(
            spk_audio_path=str(ref), prompt_audio_path=str(ref),
            prompt_audio_text=config.TTS_PROMPT_TEXT,
            text="你好呀，我是初音未来。", text_language="zh", prompt_language="zh",
        )
        step("+ 首次推理(含按需加载)")

    peak = torch.cuda.max_memory_allocated() / 1024**3
    print(f"  {'显存峰值':<32} allocated {peak:5.2f} GB")
    print(f"  GPU 实际占用: {gpu_used_mb()}")

    del engine
    gc.collect()
    torch.cuda.empty_cache()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
