"""用新的 v4c 参考音频跑一次 GPT-SoVITS 合成，并与旧参考对比。

流程：播放参考音频 → 合成同一句话 → 播放结果。
参考音频与 prompt 文本通过环境变量传入（load_dotenv 不覆盖已有环境变量），
因此**不需要改动 .env**。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "backend"))

TEST_TEXT = "主人早上好呀，今天想和 Miku 一起做什么呢？"


def play(path: Path, label: str) -> None:
    import sounddevice as sd
    import soundfile as sf

    data, sr = sf.read(str(path), always_2d=True)
    print(f"  ▶ 播放{label}：{path.name}  ({len(data)/sr:.1f}s)")
    sd.play(data[:, :2] if data.shape[1] >= 2 else data, sr)
    time.sleep(len(data) / sr + 0.4)
    sd.stop()


def main() -> int:
    # 参考音频与 prompt 文本从 sidecar 文件读，避免走 shell 传中文导致编码问题。
    # 必须在 import config / 构造 TextToSpeech 之前写进 os.environ ——
    # _spawn_server() 会 os.environ.copy() 把它传给合成服务子进程。
    ref = BASE / "assets" / "voice" / "miku_v4c" / "miku_v4c_ref_5s.wav"
    txt = ref.with_suffix(".txt")
    if not ref.exists():
        print(f"  找不到参考音频：{ref}")
        return 1
    prompt = txt.read_text(encoding="utf-8").strip() if txt.exists() else ""
    if not prompt:
        print(f"  找不到或为空：{txt}")
        return 1

    os.environ["TTS_REF_AUDIO"] = str(ref)
    os.environ["TTS_PROMPT_TEXT"] = prompt
    os.environ["TTS_PROMPT_AUDIO"] = str(ref)

    print(f"  参考音频 : {ref.relative_to(BASE)}")
    print(f"  prompt   : {prompt}")

    if "--play-ref" in sys.argv:
        play(ref, "参考音频")

    from tts import TextToSpeech

    tts = TextToSpeech()
    print(f"  engine={tts.engine}  status={tts.status}")
    if tts.status != "ready":
        print(f"  服务不可用：{getattr(tts, 'status_detail', '')}")
        tts.shutdown()
        return 1

    print(f"  正在合成：{TEST_TEXT}")
    t0 = time.time()
    result = tts.synthesize(TEST_TEXT, "HAPPY")
    dt = time.time() - t0
    if not result:
        print("  合成失败")
        tts.shutdown()
        return 1
    path, dur = result
    print(f"  OK {dt:.2f}s 完成，音频 {dur:.2f}s  (RTF {dt/max(dur,1e-6):.2f})")

    out = BASE / ".tmp" / "audio" / "sovits_v4c_test.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    import shutil

    shutil.copy(path, out)
    print(f"  已另存   : {out.relative_to(BASE)}")

    if "--play-out" in sys.argv:
        play(out, "合成结果")

    tts.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
