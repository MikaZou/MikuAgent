"""诊断 sovits TTS 引擎：打印完整堆栈。

用于定位 App 里预加载失败、但独立脚本却成功的情况。

用法:
    python tools/diag_tts.py            # 只导入 tts（模拟 App 的 TextToSpeech）
    python tools/diag_tts.py --with-stt # 先导入 stt 再导入 tts（App 的真实顺序）
"""
from __future__ import annotations

import sys
import threading
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "backend"))


def main() -> int:
    with_stt = "--with-stt" in sys.argv
    full = "--full" in sys.argv

    import config

    if with_stt or full:
        print("--- 先导入 stt（App 的真实顺序）---")
        import stt  # noqa: F401

        print("    stt 导入完成")
        if full:
            print("    构造 SpeechToText（会启动 Whisper 加载线程）...")
            stt.SpeechToText()
            print("    SpeechToText 构造完成，Whisper 正在后台加载")

    from tts import TextToSpeech

    print(f"engine={config.TTS_ENGINE} device={config.TTS_DEVICE} bert={config.TTS_USE_BERT}")

    # 绕过 __init__ 里的后台预加载线程，改成这里同步调用，方便看堆栈
    inst = TextToSpeech.__new__(TextToSpeech)
    inst.engine = config.TTS_ENGINE
    inst.voice = config.TTS_VOICE
    inst.base_rate = config.TTS_RATE
    inst.base_pitch = config.TTS_PITCH
    inst.volume = config.TTS_VOLUME
    inst.device = config.TTS_DEVICE
    inst.max_chars = config.TTS_MAX_CHARS
    inst.cache_dir = Path(config.TTS_CACHE_DIR)
    inst._lock = threading.Lock()
    inst._status = "idle"
    inst._detail = ""
    inst._gsv = None
    inst._ref_path = None
    inst._prompt_path = None

    try:
        inst._ensure_gsv()
        print(">>> _ensure_gsv OK")
        return 0
    except Exception:
        print(">>> _ensure_gsv 失败，完整堆栈：")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
