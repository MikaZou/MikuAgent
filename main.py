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


def _reload_config():
    """让设置向导刚写进 .env 的值在**本次启动**就生效。

    config 是在模块导入时读环境变量的，向导写文件发生在导入之后，
    所以必须重新 load_dotenv(override=True) 再 reload 模块。
    必须在构造 MikuAgent / TextToSpeech / SpeechToText **之前**调用。
    """
    import importlib

    from dotenv import load_dotenv

    import config

    load_dotenv(config.BASE_DIR / ".env", override=True)
    importlib.reload(config)
    return config


def _run_setup_if_needed(nickname_out: dict) -> None:
    """首次启动时弹设置向导，把选择写回 .env。"""
    from PySide6.QtWidgets import QDialog

    from ui.settings_dialog import SettingsDialog
    from ui.setup_wizard import SetupWizard

    if SettingsDialog.setup_done():
        return

    wizard = SetupWizard()
    accepted = wizard.exec() == QDialog.DialogCode.Accepted
    if accepted:
        changed = wizard.apply()
        print(f"[Setup] 设置向导已写入 .env：{changed}")
        nickname_out["value"] = wizard.nickname.text().strip()
    else:
        print("[Setup] 用户跳过了设置向导，使用 .env 现有配置")
    # 无论是否接受都标记完成，避免每次启动都弹
    SettingsDialog.mark_setup_done()


def main() -> int:
    import live2d.v3 as live2d
    from PySide6.QtWidgets import QApplication

    live2d.init()

    app = QApplication(sys.argv)
    app.setApplicationName("MikuAgent")
    app.setApplicationDisplayName("MikuAgent · 初音未来桌宠")
    # 关掉最后一个窗口不退出：桌宠主要靠托盘活着
    app.setQuitOnLastWindowClosed(False)

    # ---- 首次启动向导（必须在读 config 之前）----
    nickname: dict = {}
    _run_setup_if_needed(nickname)
    config = _reload_config()

    from agent import MikuAgent
    from memory import MemoryStore
    from stt import SpeechToText
    from tts import TextToSpeech
    from ui.pet_window import PetWindow

    memory = MemoryStore(config.DB_PATH)

    # 向导里填了称呼就顺手存进记忆库（和设置面板是同一个入口）
    if nickname.get("value"):
        try:
            memory.set_meta("user_name", nickname["value"])
            print(f"[Setup] 已记住称呼：{nickname['value']}")
        except Exception as exc:  # noqa: BLE001
            print(f"[Setup] 保存称呼失败：{exc}")

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
