"""MikuAgent · 初音未来原生桌宠（PySide6 + Live2D Cubism Native，无 Web Engine）。

用法：
    .venv\\Scripts\\python.exe main.py
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
# backend/ 里的模块用的是顶层导入（import config / from memory import ...），
# 所以要把 backend/ 放进 sys.path
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "backend"))


def main() -> int:
    import live2d.v3 as live2d
    from PySide6.QtWidgets import QApplication

    import config
    from agent import MikuAgent
    from memory import MemoryStore
    from stt import SpeechToText
    from tts import TextToSpeech
    from ui.pet_window import PetWindow

    live2d.init()

    app = QApplication(sys.argv)
    app.setApplicationName("MikuAgent")
    app.setApplicationDisplayName("MikuAgent · 初音未来桌宠")
    # 关掉最后一个窗口不退出：桌宠主要靠托盘活着
    app.setQuitOnLastWindowClosed(False)

    memory = MemoryStore(config.DB_PATH)
    agent = MikuAgent(memory)
    tts = TextToSpeech()
    stt = SpeechToText()

    window = PetWindow(memory, agent, stt, tts)
    window.start()

    code = app.exec()
    live2d.dispose()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
