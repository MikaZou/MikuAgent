"""端到端自检：对话 → 情感 → 动作/表情 → 气泡 → TTS → 播放 → 口型。

用法：
    .venv\\Scripts\\python.exe tools\\selftest_chat.py "要发送的话"
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "backend"))

MODEL_MESSAGE = "你好呀 Miku！我叫小林，最喜欢吃葱油饼，今天心情特别好呢～"

log: list[str] = []


def main() -> int:
    message = sys.argv[1] if len(sys.argv) > 1 else MODEL_MESSAGE
    hold = 13.0
    if "--hold" in sys.argv:
        try:
            hold = float(sys.argv[sys.argv.index("--hold") + 1])
        except (IndexError, ValueError):
            pass

    import live2d.v3 as live2d
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    import config
    from agent import MikuAgent
    from memory import MemoryStore
    from stt import SpeechToText
    from tts import TextToSpeech
    from ui.pet_window import PetWindow

    live2d.init()
    app = QApplication(sys.argv)

    memory = MemoryStore(config.DB_PATH)
    agent = MikuAgent(memory)
    tts = TextToSpeech()
    stt = SpeechToText()
    win = PetWindow(memory, agent, stt, tts)
    win.show()

    out_dir = BASE_DIR / ".tmp"
    out_dir.mkdir(exist_ok=True)
    state = {"t0": time.time(), "shots": 0, "reply": None, "tts": None}

    print(f"[selftest] agent.live={agent.live}  api_key={config.HAS_API_KEY}")
    print(f"[selftest] tts engine={tts.engine} status={tts.status}")

    # 记录关键事件
    orig_on_reply = win._on_reply

    def spy_reply(result):
        state["reply"] = result
        print(f"[selftest] REPLY emotion={result.get('emotion')} len={len(result.get('reply',''))}")
        print(f"[selftest]   text={result.get('reply','')[:80]}")
        orig_on_reply(result)

    win._on_reply = spy_reply  # type: ignore[method-assign]

    orig_tts = win._on_tts_ready

    def spy_tts(path, duration):
        state["tts"] = (path, duration)
        print(f"[selftest] TTS wav={Path(path).name if path else '(none)'} dur={duration:.2f}s")
        orig_tts(path, duration)
        print(f"[selftest] speaking={win.speaking}")

    win._on_tts_ready = spy_tts  # type: ignore[method-assign]

    def shot(tag: str):
        state["shots"] += 1
        p = out_dir / f"selftest_{state['shots']}_{tag}.png"
        win.grabFramebuffer().save(str(p))
        print(f"[selftest] shot -> {p.name}  ({time.time()-state['t0']:.1f}s)")

    QTimer.singleShot(1200, lambda: win.send_message(message))
    QTimer.singleShot(int(min(6.0, hold * 0.4) * 1000), lambda: shot("mid"))
    QTimer.singleShot(int((hold - 1.0) * 1000), lambda: shot("final"))
    QTimer.singleShot(int(hold * 1000), app.quit)

    app.exec()

    print("\n=== SUMMARY ===")
    print(f"  reply ok      : {bool(state['reply'])}")
    print(f"  tts produced  : {bool(state['tts'] and state['tts'][0])}")
    print(f"  lipsync used  : {win._wav is not None}")
    live2d.dispose()
    return 0 if state["reply"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
