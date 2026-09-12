"""只跑远程服务（不开桌宠窗口），用于联调手机端。

桌宠本体不受影响；这个脚本只是把同一套 backend 拉起来 +
WebSocket 服务，方便在没有图形界面的情况下测手机端。

用法::

    python tools/run_remote_only.py            # 常驻
    python tools/run_remote_only.py --seconds 120
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "backend"))

import config  # noqa: E402


def main() -> int:
    seconds = 0
    if "--seconds" in sys.argv:
        seconds = int(sys.argv[sys.argv.index("--seconds") + 1])

    from agent import MikuAgent
    from memory import MemoryStore
    from remote_server import RemoteServer
    from stt import SpeechToText
    from tts import TextToSpeech

    memory = MemoryStore(config.DB_PATH)
    agent = MikuAgent(memory)
    tts = TextToSpeech()
    stt = SpeechToText()

    srv = RemoteServer(agent, tts, stt, memory)
    print(f"[remote-only] provider = {srv.provider_info()}", flush=True)
    srv.start()

    try:
        if seconds > 0:
            time.sleep(seconds)
        else:
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            tts.shutdown()
        except Exception:  # noqa: BLE001
            pass
        srv.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
