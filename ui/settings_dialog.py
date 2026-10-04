"""设置窗口：运行状态 + 模型切换 + 语音开关 + 完全退出。

这个窗口同时充当「控制台」—— 它在启动时第一个出现。为什么不另做一个
控制台主页：模型切换、语音开关、退出本来就是一回事，分成两个窗口只会
让「设置到底在哪儿改」变得更含糊。所以这里只做**一个**设置窗口，
但它承载了原来分散在三个地方（设置面板 / 托盘菜单 / 只有悬停才出现的角标按钮）
才能碰到的东西，尤其是「完全退出」。

为什么要专门给「完全退出」一个按钮：

桌宠主窗口是**无边框 + 置顶 + 不进任务栏**的（Qt.Tool）。用户把窗口关掉之后，
如果进程没有真的退出，他就再也找不到它了 —— 任务栏里没有，只能去任务管理器。
所以退出必须是显式的、看得见的，并且要真的把 TTS 子进程、摄像头、
Live2D 显存一并收干净（见 ui/app_control.py）。
"""
from __future__ import annotations

from typing import Optional

import models_catalog
from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

import config

ORG = "MikuAgent"
APP = "MikuAgent"

QSS = """
QDialog { background: #f7f9fa; }
QLabel { color: #1f2430; font-size: 13px; }
QLabel#tip { color: #6b7280; font-size: 12px; }
QLabel#section { font-size: 13px; font-weight: 700; color: #0f766e; }
QLabel#value { font-size: 13px; color: #1f2430; }
QLabel#key { color: #6b7280; font-size: 12px; }
QFrame#card { background: #ffffff; border: 1px solid #e3e8ea; border-radius: 10px; }
QFrame#sep { background: #e3e8ea; max-height: 1px; }
QPushButton {
    background: #39c5bb; color: #fff; border: none;
    border-radius: 8px; padding: 7px 14px; font-weight: 600;
}
QPushButton:hover { background: #2fb3a9; }
QPushButton#ghost {
    background: #ffffff; color: #0f766e;
    border: 1px solid #99e0da; font-weight: 600;
    padding: 7px 12px;
}
QPushButton#ghost:hover { background: #eafaf8; }
QPushButton#danger { background: #ef4444; }
QPushButton#danger:hover { background: #dc2626; }
QRadioButton { font-size: 13px; padding: 2px 0; }
QCheckBox { font-size: 13px; }
"""


class ModelPicker(QFrame):
    """一组模型单选项（带说明），桌面端 / 手机端各一个。"""

    changed = Signal(str)

    def __init__(self, title: str, tip: str) -> None:
        super().__init__()
        self.setObjectName("card")
        self._group = QButtonGroup(self)
        self._buttons: dict[str, QRadioButton] = {}
        self._loading = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(6)
        head = QLabel(title)
        head.setObjectName("section")
        lay.addWidget(head)

        for m in models_catalog.MODELS:
            btn = QRadioButton(f"{m['name']}　{m['note']}")
            self._group.addButton(btn)
            self._buttons[m["id"]] = btn
            lay.addWidget(btn)
            btn.toggled.connect(self._on_toggled)

        hint = QLabel(tip)
        hint.setObjectName("tip")
        hint.setWordWrap(True)
        lay.addWidget(hint)

    def _on_toggled(self, checked: bool) -> None:
        if not checked or self._loading:
            return
        for mid, btn in self._buttons.items():
            if btn.isChecked():
                self.changed.emit(mid)
                return

    def set_state(self, model_id: str, unavailable: set[str]) -> None:
        self._loading = True
        try:
            for mid, btn in self._buttons.items():
                entry = models_catalog.get(mid) or {}
                btn.setEnabled(mid not in unavailable)
                btn.setText(
                    f"{entry.get('name', mid)}　{entry.get('note', '')}"
                    + ("　（本地没有这个模型）" if mid in unavailable else "")
                )
                btn.setChecked(mid == model_id)
        finally:
            self._loading = False


class SettingsDialog(QDialog):
    """运行状态 + 模型切换 + 语音开关 + 完全退出。"""

    nickname_saved = Signal(str)
    tts_toggled = Signal(bool)
    stt_toggled = Signal(bool)
    video_toggled = Signal(bool)
    # 请求打开「首次设置向导」的编辑模式（改引擎 / API Key / 手机端等）
    reconfigure_requested = Signal()
    # 换模型 / 完全退出（由 main.py 接线到实际的加载与退出逻辑）
    desktop_model_changed = Signal(str)
    watermark_changed = Signal(bool)
    pet_visibility_requested = Signal(bool)
    quit_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("MikuAgent · 设置")
        self.setMinimumWidth(440)
        self.setStyleSheet(QSS)
        # 不加父窗口 + 自己也是「置顶」：桌宠窗口是 WindowStaysOnTopHint 的，
        # 普通窗口永远会被它盖住；而这个窗口在启动时第一个出现，被盖住就等于
        # 用户找不到设置入口。两个都置顶时，谁被激活谁在上面。
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.settings = QSettings(ORG, APP)
        self._loading = False
        self._pet_visible = True

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        # ---------------- 运行状态 ----------------
        status = QFrame()
        status.setObjectName("card")
        status_lay = QVBoxLayout(status)
        status_lay.setContentsMargins(14, 12, 14, 12)
        status_lay.setSpacing(5)
        self._rows: dict[str, QLabel] = {}
        for key, label in (
            ("pet", "运行状态"),
            ("model", "Live2D 模型"),
            ("phone", "手机端"),
            ("voice", "语音通道"),
            ("session", "当前会话"),
            ("mode", "运行模式"),
            ("tts", "语音输出"),
            ("stt", "语音输入"),
        ):
            row = QHBoxLayout()
            row.setSpacing(8)
            k = QLabel(label)
            k.setObjectName("key")
            k.setFixedWidth(76)
            v = QLabel("-")
            v.setObjectName("value")
            v.setWordWrap(True)
            row.addWidget(k)
            row.addWidget(v, 1)
            status_lay.addLayout(row)
            self._rows[key] = v
        root.addWidget(status)

        self._key_row_key = self._rows["model"]

        # ---------------- 模型 ----------------
        # 只留桌面端。**手机端用哪个模型由手机自己定** —— 换模型是手机上
        # 顺手就做的事，跑到电脑上来改反而绕；PC 这边只负责把所有模型都按 id
        # 提供出去（见 backend/remote_server.py 的 /model/<id>/...），
        # 以及把手机报上来的选择记一笔（data/model_prefs.json 的 phone）。
        self.picker_desktop = ModelPicker(
            "桌面端模型",
            "换完立刻重建 Live2D 渲染器；两个模型的美术范围差很多，取景会自动重新拟合。",
        )
        self.picker_desktop.changed.connect(self.desktop_model_changed.emit)
        root.addWidget(self.picker_desktop)

        # ---------------- 开关 ----------------
        self._tts_check = QCheckBox("启用语音输出（Miku 说话）")
        self._stt_check = QCheckBox("启用语音输入（按住说话）")
        self._video_check = QCheckBox("视频对话（让 Miku 在每轮语音里看见你）")
        self._watermark_check = QCheckBox("显示模型水印")
        root.addWidget(self._tts_check)
        root.addWidget(self._stt_check)
        root.addWidget(self._video_check)
        root.addWidget(self._watermark_check)

        self._watermark_tip = QLabel("")
        self._watermark_tip.setObjectName("tip")
        self._watermark_tip.setWordWrap(True)
        root.addWidget(self._watermark_tip)

        vision_tip = QLabel(
            "📷 开启后摄像头会保持打开（指示灯常亮），但只在你说完话的那一刻抓一帧上传给 "
            "DeepSeek，不是持续上传。画面不写入本地磁盘。"
        )
        vision_tip.setObjectName("tip")
        vision_tip.setWordWrap(True)
        root.addWidget(vision_tip)

        # ---------------- 称呼 ----------------
        nick_row = QHBoxLayout()
        nick_row.addWidget(QLabel("称呼"))
        self._nickname = QLineEdit()
        self._nickname.setPlaceholderText("例如：主人 / 小名")
        save_btn = QPushButton("保存称呼")
        save_btn.clicked.connect(self._on_save_nickname)
        nick_row.addWidget(self._nickname, 1)
        nick_row.addWidget(save_btn)
        root.addLayout(nick_row)

        # ---------------- 底部按钮 ----------------
        sep = QFrame()
        sep.setObjectName("sep")
        sep.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep)

        self.btn_toggle_pet = QPushButton("隐藏 Miku")
        self.btn_toggle_pet.setObjectName("ghost")
        reconf_btn = QPushButton("⚙ 修改配置")
        reconf_btn.setObjectName("ghost")
        reconf_btn.setToolTip(
            "重新打开首次设置向导：语音引擎（本地/云端）、API Key、"
            "视频对话、手机端。保存后立即生效，无需重启。"
        )
        self.btn_quit = QPushButton("完全退出 MikuAgent")
        self.btn_quit.setObjectName("danger")
        self.btn_quit.setToolTip(
            "停掉语音合成、松开摄像头、释放 Live2D 占用的显存，并退出整个进程"
        )

        bottom = QHBoxLayout()
        bottom.addWidget(self.btn_toggle_pet)
        bottom.addWidget(reconf_btn)
        bottom.addStretch(1)
        bottom.addWidget(self.btn_quit)
        root.addLayout(bottom)

        tip = QLabel(
            "💡 桌宠主窗口是无边框、不进任务栏的，所以关掉它请用「完全退出」，"
            "或者右键托盘图标。只想让它消失一会儿就点「隐藏 Miku」，"
            "之后从托盘或这个窗口都能叫回来。"
        )
        tip.setObjectName("tip")
        tip.setWordWrap(True)
        root.addWidget(tip)

        self.btn_toggle_pet.clicked.connect(
            lambda: self.pet_visibility_requested.emit(not self._pet_visible)
        )
        self.btn_quit.clicked.connect(self._confirm_quit)
        reconf_btn.clicked.connect(self.reconfigure_requested.emit)
        self._tts_check.toggled.connect(self._on_tts)
        self._stt_check.toggled.connect(self._on_stt)
        self._video_check.toggled.connect(self._on_video)
        self._watermark_check.toggled.connect(self._on_watermark)

    # ------------------------------------------------------------------ 数据
    def load_state(
        self,
        health: dict,
        stt_status: str,
        tts_status: str,
        nickname: str,
        console: Optional[dict] = None,
    ) -> None:
        self._loading = True
        try:
            self._rows["mode"].setText(
                "演示模式（未连接）" if health.get("mock") else
                f"已连接 DeepSeek（Key {'已配置' if health.get('has_api_key') else '未配置'}）"
            )
            stt_text = {
                "ready": "就绪",
                "loading": "模型加载中（首次需联网下载）",
                "error": "加载失败，见控制台日志",
            }.get(stt_status, stt_status)
            tts_text = {
                "ready": "就绪",
                "loading": "加载中",
                "error": "合成失败，见控制台日志",
                "unavailable": "不可用（未安装依赖）",
                "disabled": "已关闭",
                "idle": "待命",
            }.get(tts_status, tts_status)
            self._rows["stt"].setText(f"{health.get('stt_model', '-')} · {stt_text}")
            self._rows["tts"].setText(f"{health.get('tts_engine', '-')} · {tts_text}")

            self._tts_check.setChecked(bool(self.settings.value("tts_enabled", True, type=bool)))
            self._stt_check.setChecked(bool(self.settings.value("stt_enabled", True, type=bool)))
            self._video_check.setChecked(
                bool(self.settings.value("video_enabled", config.VISION_ENABLED, type=bool))
            )
            self._nickname.setText(nickname or "")

            if console:
                self.apply_console_state(console)
        finally:
            self._loading = False

    def apply_console_state(self, state: dict) -> None:
        """刷新模型选择、手机端地址、会话等由主程序组装的状态。"""
        self._loading = True
        try:
            self.set_pet_visible(bool(state.get("pet_visible", True)))
            self._rows["pet"].setText(
                "运行中（显示中）" if state.get("pet_visible", True)
                else "运行中（已隐藏 — 从托盘或这里叫回来）"
            )
            for key in ("model", "phone", "voice", "session"):
                if state.get(key + "_text") is not None:
                    self._rows[key].setText(state[key + "_text"])

            unavailable = set(state.get("missing_models") or [])
            self.picker_desktop.set_state(state.get("desktop_model", ""), unavailable)

            wm_param = state.get("watermark_param")
            self._watermark_check.setEnabled(bool(wm_param))
            self._watermark_check.setChecked(bool(state.get("watermark_visible", False)))
            self._watermark_tip.setText(
                f"当前模型的水印参数是 {wm_param}；关掉之后水印不会再出现。"
                if wm_param else
                "当前模型没有水印参数，这个选项对它无效。"
            )
        finally:
            self._loading = False

    def set_pet_visible(self, visible: bool) -> None:
        self._pet_visible = bool(visible)
        self.btn_toggle_pet.setText("隐藏 Miku" if self._pet_visible else "显示 Miku")

    # ------------------------------------------------------------------ 槽
    def _on_tts(self, checked: bool) -> None:
        self.settings.setValue("tts_enabled", checked)
        if not self._loading:
            self.tts_toggled.emit(checked)

    def _on_stt(self, checked: bool) -> None:
        self.settings.setValue("stt_enabled", checked)
        if not self._loading:
            self.stt_toggled.emit(checked)

    def _on_video(self, checked: bool) -> None:
        self.settings.setValue("video_enabled", checked)
        if not self._loading:
            self.video_toggled.emit(checked)

    def _on_watermark(self, checked: bool) -> None:
        if not self._loading:
            self.watermark_changed.emit(checked)

    def _on_save_nickname(self) -> None:
        self.nickname_saved.emit(self._nickname.text().strip())

    def _confirm_quit(self) -> None:
        """二次确认。

        桌宠是长期挂着的东西，误点「完全退出」等于把 Miku 关了；而且它不像
        普通应用那样能在任务栏里点回来，所以这里必须问一句。
        """
        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox(self)
        box.setWindowTitle("完全退出")
        box.setText("要关掉 Miku 吗？")
        box.setInformativeText(
            "会停掉语音合成、松开摄像头、释放 Live2D 占用的显存，并退出整个进程。\n"
            "下次启动双击 start.bat 即可。"
        )
        box.setIcon(QMessageBox.Icon.Question)
        yes = box.addButton("完全退出", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is yes:
            self.quit_requested.emit()

    # 关闭设置窗口 ≠ 退出程序：从托盘或角标 ⚙ 都能再打开
    def closeEvent(self, event) -> None:  # noqa: N802
        event.accept()

    # ---------------------------------------------------------------- 静态读
    @staticmethod
    def tts_enabled() -> bool:
        return bool(QSettings(ORG, APP).value("tts_enabled", True, type=bool))

    @staticmethod
    def stt_enabled() -> bool:
        return bool(QSettings(ORG, APP).value("stt_enabled", True, type=bool))

    @staticmethod
    def video_enabled() -> bool:
        return bool(QSettings(ORG, APP).value("video_enabled", config.VISION_ENABLED, type=bool))

    @staticmethod
    def watermark_visible() -> bool:
        """是否显示模型水印。

        默认 **不显示**：模型说明第 4 条要求「水印按键默认打开」，但用户明确
        要求去掉水印，所以这里反过来默认关。开关本身留着，想恢复点一下就行。
        """
        return bool(QSettings(ORG, APP).value("watermark_visible", False, type=bool))

    @staticmethod
    def set_watermark_visible(visible: bool) -> None:
        QSettings(ORG, APP).setValue("watermark_visible", bool(visible))

    @staticmethod
    def setup_done() -> bool:
        """是否已经跑过首次设置向导。"""
        return bool(QSettings(ORG, APP).value("setup_done", False, type=bool))

    @staticmethod
    def mark_setup_done() -> None:
        QSettings(ORG, APP).setValue("setup_done", True)
