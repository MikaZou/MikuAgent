"""端到端验证「设置面板 → 修改配置」整条链路（真实 PetWindow）。

链路：点按钮 → 打开向导(编辑模式) → 写 .env → reload config
      → tts.reconfigure() → stt.reconfigure() → 远程服务 sync → 刷新面板

为什么要用真实 PetWindow：这条链路的坑都在「接线」上（信号漏接、
reload 了 config 但没通知组件、RemoteServer 还拿着旧引用）。
只测单个函数是测不出来的。

模态对话框用替身顶掉（否则测试会卡在 exec() 等人点按钮），
但替身的 apply() **真的写 .env**，这样 reload 路径是真实的。
测试结束会把 .env 原样还原。

用法：python tools/test_reconfigure_e2e.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

import config  # noqa: E402
from backend import envfile  # noqa: E402

ENV_PATH = ROOT / ".env"
failures: list[str] = []


def main() -> int:
    original_env = ENV_PATH.read_text(encoding="utf-8")
    before = envfile.read_env()

    import live2d.v3 as live2d
    from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QLineEdit

    live2d.init()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    from agent import MikuAgent
    from memory import MemoryStore
    from stt import SpeechToText
    from tts import TextToSpeech
    from ui.pet_window import PetWindow

    memory = MemoryStore(config.DB_PATH)
    agent = MikuAgent(memory)
    tts = TextToSpeech()
    stt = SpeechToText()
    window = PetWindow(memory, agent, stt, tts)
    window.start()

    # ---- 替身向导：不弹窗，但真的写 .env ----
    import ui.setup_wizard as sw

    new_engine = "sovits" if before.get("TTS_ENGINE") != "sovits" else "minimax"
    new_stt = "local-whisper" if before.get("STT_TRANSCRIBER") != "local-whisper" else "minimax"
    print(f"切换目标：TTS {before.get('TTS_ENGINE')} → {new_engine} | "
          f"STT {before.get('STT_TRANSCRIBER')} → {new_stt}")

    class FakeWizard:
        def __init__(self, parent=None, edit_mode=False):
            self.edit_mode = edit_mode
            self.nickname = QLineEdit("")
            self.video = QCheckBox()
            self.video.setChecked(before.get("VISION_ENABLED", "false") == "true")
            self.prefilled = None

        def exec(self):
            # open_reconfigure 会先用记忆库里的值回填称呼栏，所以这里才读得到；
            # 然后模拟用户改成新名字（在回填之后，才是真实时序）
            self.prefilled = self.nickname.text()
            self.nickname.setText("测试称呼")
            return QDialog.DialogCode.Accepted

        def apply(self):
            values = {
                "TTS_ENGINE": new_engine,
                "STT_TRANSCRIBER": new_stt,
                "DEEPSEEK_API_KEY": before.get("DEEPSEEK_API_KEY", ""),
                "MINIMAX_API_KEY": before.get("MINIMAX_API_KEY", ""),
                "VISION_ENABLED": "true" if self.video.isChecked() else "false",
                "REMOTE_ENABLED": before.get("REMOTE_ENABLED", "false"),
            }
            return envfile.update_env(values)

    real_wizard = sw.SetupWizard
    sw.SetupWizard = FakeWizard
    # 先放一个旧称呼，用来验证「回填」这一半
    memory.set_meta("user_name", "旧称呼")
    # pet_window 里是 `from ui.setup_wizard import SetupWizard`，函数内导入，
    # 所以替换模块属性即可命中
    holder = {}
    try:
        _orig_init = FakeWizard.__init__

        def _capture(self, parent=None, edit_mode=False):
            _orig_init(self, parent, edit_mode)
            holder["wizard"] = self

        FakeWizard.__init__ = _capture
        # ---- 触发 ----
        window.open_reconfigure()
        app.processEvents()
    finally:
        sw.SetupWizard = real_wizard

    wiz = holder.get("wizard")
    print(f"称呼栏回填值: {getattr(wiz, 'prefilled', None)!r}（期望 '旧称呼'）")
    if getattr(wiz, "prefilled", None) != "旧称呼":
        failures.append(f"称呼没有从记忆库回填，实际 {getattr(wiz, 'prefilled', None)!r}")

    # ---- 断言 ----
    print()
    print("=" * 70)
    print(f"TTS engine   : {tts.engine}（期望 {new_engine}）")
    if tts.engine != new_engine:
        failures.append(f"TTS engine 未切换：{tts.engine} != {new_engine}")

    print(f"STT transcriber: {stt.transcriber}（期望 {new_stt}）")
    if stt.transcriber != new_stt:
        failures.append(f"STT transcriber 未切换：{stt.transcriber} != {new_stt}")

    print(f"config.TTS_ENGINE: {config.TTS_ENGINE}（期望 {new_engine}）")
    if config.TTS_ENGINE != new_engine:
        failures.append("reload_config 没生效：config.TTS_ENGINE 还是旧值")

    # 面板显示应已刷新
    shown = window.settings_dialog._tts_status.text()
    print(f"面板显示语音输出引擎: {shown!r}")
    if new_engine not in shown:
        failures.append(f"设置面板没刷新，仍显示 {shown!r}")

    nickname = memory.get_meta("user_name")
    print(f"称呼已保存: {nickname!r}")
    if nickname != "测试称呼":
        failures.append(f"称呼没写进记忆库：{nickname!r}")

    if new_engine == "sovits":
        print(f"sovits 预热状态: {tts.status}（应为 loading）")
        if tts.status != "loading":
            failures.append(f"切到 sovits 后 status 应为 loading，实际 {tts.status}")

    # ---- 收尾：还原 .env 并退出 ----
    ENV_PATH.write_text(original_env, encoding="utf-8")
    restored = envfile.read_env()
    print()
    print(f".env 已还原: TTS_ENGINE={restored.get('TTS_ENGINE')} "
          f"STT_TRANSCRIBER={restored.get('STT_TRANSCRIBER')}")
    if restored.get("TTS_ENGINE") != before.get("TTS_ENGINE"):
        failures.append(".env 还原失败")

    try:
        tts.shutdown()
        window.quit_app()
    except Exception:  # noqa: BLE001
        pass
    app.processEvents()

    print()
    print("=" * 70)
    if failures:
        print(f"失败 {len(failures)} 项：")
        for f in failures:
            print(f"  [X] {f}")
        return 1
    print("端到端链路全部通过 [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
