"""验证设置面板的「修改配置」入口，以及远程服务的运行中启停/换端口。

两个独立的易错点：
  1. 按钮 → 信号 是否真的接上了（Qt 信号漏接是静默失败）
  2. RemoteController 在**同一个进程里**停掉再起来时，
     端口有没有真的释放 —— 不等旧线程退出就 bind 会「端口被占用」，
     而且 start() 是异步的，失败只打日志，调用方看不到

用法：python tools/test_reconfigure_ui.py
"""
from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

failures: list[str] = []


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.6):
            return True
    except OSError:
        return False


def wait_port(port: int, want: bool, timeout: float = 12.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(port) == want:
            return True
        time.sleep(0.4)
    return False


def test_signal() -> None:
    print("=" * 70)
    print("测试 1：设置面板「修改配置」按钮 → reconfigure_requested 信号")
    print("=" * 70)
    from PySide6.QtWidgets import QApplication, QPushButton

    from ui.settings_dialog import SettingsDialog

    app = QApplication.instance() or QApplication(sys.argv)
    dlg = SettingsDialog(None)
    fired: list[int] = []
    dlg.reconfigure_requested.connect(lambda: fired.append(1))

    btn = dlg.findChild(QPushButton, "ghost")
    if btn is None:
        failures.append("设置面板里找不到 objectName='ghost' 的「修改配置」按钮")
        print("  [X] 找不到按钮")
        return
    print(f"  找到按钮：{btn.text()!r}")
    btn.click()
    app.processEvents()
    if fired:
        print("  [OK] 点击后信号已发出")
    else:
        failures.append("点击「修改配置」按钮没有发出 reconfigure_requested 信号")
        print("  [X] 信号没发出")


def test_remote_controller() -> None:
    print()
    print("=" * 70)
    print("测试 2：远程服务运行中 开 → 换端口 → 关")
    print("=" * 70)
    import config
    from agent import MikuAgent
    from memory import MemoryStore
    from remote_server import RemoteController
    from stt import SpeechToText
    from tts import TextToSpeech

    mem = MemoryStore(config.DB_PATH)
    agent = MikuAgent(mem)
    tts = TextToSpeech()
    stt = SpeechToText()
    ctrl = RemoteController(agent, tts, stt, mem)

    p1 = int(config.REMOTE_PORT)
    p2 = p1 + 7

    # --- 关闭状态：sync 应无动作 ---
    config.REMOTE_ENABLED = False
    r = ctrl.sync()
    print(f"  关闭状态 sync -> {r!r}")
    if r != "unchanged":
        failures.append(f"关闭状态下 sync 应为 unchanged，实际 {r}")
    if port_open(p1):
        failures.append(f"关闭状态下端口 {p1} 竟然被占用（可能上次没释放）")

    # --- 开启 ---
    config.REMOTE_ENABLED = True
    r = ctrl.sync()
    print(f"  开启 sync -> {r!r}")
    if r != "started":
        failures.append(f"开启时 sync 应为 started，实际 {r}")
    if wait_port(p1, True):
        print(f"  [OK] 端口 {p1} 已监听")
    else:
        failures.append(f"开启后端口 {p1} 12s 内没有监听")
        return

    # --- 幂等：再 sync 不该重启 ---
    r = ctrl.sync()
    print(f"  重复 sync -> {r!r}（应为 unchanged，不该重启）")
    if r != "unchanged":
        failures.append(f"配置没变时 sync 应为 unchanged，实际 {r}")

    # --- 换端口：必须重启且新端口可用 ---
    config.REMOTE_PORT = p2
    r = ctrl.sync()
    print(f"  换端口 {p1}->{p2} sync -> {r!r}")
    if r != "restarted":
        failures.append(f"换端口时 sync 应为 restarted，实际 {r}")
    if wait_port(p2, True):
        print(f"  [OK] 新端口 {p2} 已监听")
    else:
        failures.append(f"换端口后 {p2} 没起来（很可能旧端口没释放干净）")
    if port_open(p1):
        failures.append(f"换端口后旧端口 {p1} 仍在监听")
    else:
        print(f"  [OK] 旧端口 {p1} 已释放")

    # --- 关闭：端口必须释放 ---
    config.REMOTE_ENABLED = False
    r = ctrl.sync()
    print(f"  关闭 sync -> {r!r}")
    if r != "stopped":
        failures.append(f"关闭时 sync 应为 stopped，实际 {r}")
    if wait_port(p2, False, timeout=10):
        print(f"  [OK] 端口 {p2} 已释放")
    else:
        failures.append(f"关闭后端口 {p2} 仍被占用")

    ctrl.stop()
    tts.shutdown()


def main() -> int:
    test_signal()
    test_remote_controller()
    print()
    print("=" * 70)
    if failures:
        print(f"失败 {len(failures)} 项：")
        for f in failures:
            print(f"  [X] {f}")
        return 1
    print("全部通过 [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
