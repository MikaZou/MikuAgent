"""回归：设置窗口不该「自己」发出换模型信号。

为什么专门测这个：真机联调时 `data/remote.log` 里出现过
「手机端模型已切到 miku」紧接着「已切到 miku_v5」这种**来回横跳** ——
而当时没有任何人点过那个单选框。后果不轻：手机会被通知重新同步模型并
重载页面两次（贴图 30 多 MB，白下载、白闪屏）。

所以这里不起真机、不联网，只构造一个设置窗口，用程序化的状态刷它，
断言「没有真的变化就不该发信号」。

用法：.venv\\Scripts\\python.exe tools\\test_settings_dialog.py
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "backend"))

from PySide6.QtWidgets import QApplication  # noqa: E402

import models_catalog  # noqa: E402
from ui.settings_dialog import SettingsDialog  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILED.append(name)


HEALTH = {"mock": False, "has_api_key": True, "model": "deepseek-flash",
          "stt_model": "small", "tts_engine": "minimax"}


def make_state(desktop: str, phone: str, watermark: bool = False) -> dict:
    return {
        "pet_visible": True,
        "desktop_model": desktop,
        "phone_model": phone,
        "missing_models": [],
        "watermark_param": "Param137" if desktop == "miku_v5" else None,
        "watermark_visible": watermark,
        "model_text": desktop,
        "phone_text": phone,
        "voice_text": "x",
        "session_text": "#1",
    }


def main() -> int:
    app = QApplication(sys.argv[:1])
    dlg = SettingsDialog()

    desktop_events: list[str] = []
    phone_events: list[str] = []
    wm_events: list[bool] = []
    dlg.desktop_model_changed.connect(desktop_events.append)
    dlg.phone_model_changed.connect(phone_events.append)
    dlg.watermark_changed.connect(wm_events.append)

    # ---- 1) 首次装载状态：不该发出任何「换模型」信号 ----
    dlg.load_state(HEALTH, "ready", "ready", "主人", console=make_state("miku_v5", "miku_v5"))
    check("首次装载不发 desktop 信号", desktop_events == [], str(desktop_events))
    check("首次装载不发 phone 信号", phone_events == [], str(phone_events))
    check("首次装载不发 water 信号", wm_events == [], str(wm_events))

    # ---- 2) 状态没变时重复刷新：依然不该发 ----
    for _ in range(5):
        dlg.apply_console_state(make_state("miku_v5", "miku_v5"))
    check("重复刷新不发信号", desktop_events == [] and phone_events == [],
          f"desktop={desktop_events} phone={phone_events}")

    # ---- 3) 程序化同步（PC 端 / 别的手机改了）只更新显示，**不发**信号 ----
    # 发信号意味着「请求切换」，而切换本身已经发生了 —— 回声会把手机
    # 反复推去重新同步模型（贴图 30MB，白下载、白闪屏）。
    dlg.apply_console_state(make_state("miku_v5", "miku"))
    check("程序化同步 phone 不发信号", phone_events == [], str(phone_events))
    check("程序化同步会把选中项同步过来", dlg.picker_phone._buttons["miku"].isChecked())

    # ---- 4) 模拟用户点击：应该恰好发一次 ----
    # 直接 setChecked 绕开 set_state，就等价于用户点了那个单选钮。
    dlg.picker_phone._buttons["miku_v5"].setChecked(True)
    check("用户点选后恰好发一次", phone_events == ["miku_v5"], str(phone_events))
    dlg.apply_console_state(make_state("miku_v5", "miku_v5"))
    check("程序化同步同一个值不再发", phone_events == ["miku_v5"], str(phone_events))

    # ---- 5) 桌面端换模型：发 desktop 信号，且**不**误发 phone ----
    phone_events.clear()
    dlg.picker_desktop._buttons["miku"].setChecked(True)
    check("用户换桌面模型发 desktop 一次", desktop_events == ["miku"], str(desktop_events))
    check("桌面端换模型不误发 phone", phone_events == [], str(phone_events))

    # ---- 6) 程序化改水印勾选不该反过来发信号（否则会来回打架）----
    wm_events.clear()
    dlg.apply_console_state(make_state("miku", "miku_v5", watermark=True))
    check("程序化改水印勾选不发信号", wm_events == [], str(wm_events))
    check("水印勾选状态被正确回填", dlg._watermark_check.isChecked())

    # ---- 7) 没有水印参数的模型：选项禁用 ----
    dlg.apply_console_state(make_state("miku", "miku_v5", watermark=False))
    check("经典模型下水印选项被禁用", not dlg._watermark_check.isEnabled())

    # ---- 8) 本地缺失的模型要标出来且不可选 ----
    state = make_state("miku_v5", "miku_v5")
    state["missing_models"] = ["miku"]
    dlg.apply_console_state(state)
    check("缺失的模型按钮被禁用", not dlg.picker_phone._buttons["miku"].isEnabled())
    check("缺失的模型有文字提示",
          "没有这个模型" in dlg.picker_phone._buttons["miku"].text(),
          dlg.picker_phone._buttons["miku"].text())

    dlg.close()
    app.quit()

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
