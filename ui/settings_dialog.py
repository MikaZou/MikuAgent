"""设置面板：复刻网页版 renderSettings() 的信息与开关。"""
from __future__ import annotations

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

ORG = "MikuAgent"
APP = "MikuAgent"


class SettingsDialog(QDialog):
    """运行状态 + 语音开关 + 昵称。"""

    nickname_saved = Signal(str)
    tts_toggled = Signal(bool)
    stt_toggled = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("MikuAgent 设置")
        self.setMinimumWidth(380)
        self.setStyleSheet(
            """
            QDialog { background: #f7f9fa; }
            QLabel { color: #1f2430; font-size: 13px; }
            QLabel#tip { color: #6b7280; font-size: 12px; }
            QPushButton {
                background: #39c5bb; color: #fff; border: none;
                border-radius: 8px; padding: 7px 14px; font-weight: 600;
            }
            QPushButton:hover { background: #2fb3a9; }
            """
        )
        self.settings = QSettings(ORG, APP)

        self._mode = QLabel("-")
        self._key = QLabel("-")
        self._model = QLabel("-")
        self._tts_status = QLabel("-")
        self._stt_status = QLabel("-")

        self._tts_check = QCheckBox("启用语音输出（Miku 说话）")
        self._stt_check = QCheckBox("启用语音输入（按住说话）")
        self._nickname = QLineEdit()
        self._nickname.setPlaceholderText("例如：主人 / 小名")
        save_btn = QPushButton("保存称呼")

        form = QFormLayout()
        form.addRow("运行模式", self._mode)
        form.addRow("API Key", self._key)
        form.addRow("模型", self._model)
        form.addRow("语音输出引擎", self._tts_status)
        form.addRow("语音输入引擎", self._stt_status)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(10)
        root.addLayout(form)

        line = QLabel()
        line.setFixedHeight(1)
        line.setStyleSheet("background: #e3e8ea;")
        root.addWidget(line)

        root.addWidget(self._tts_check)
        root.addWidget(self._stt_check)

        nick_row = QHBoxLayout()
        nick_row.addWidget(QLabel("称呼"))
        nick_row.addWidget(self._nickname, 1)
        nick_row.addWidget(save_btn)
        root.addLayout(nick_row)

        tip = QLabel("💡 在 .env 中配置 DEEPSEEK_API_KEY / TTS_* 后重启即可生效。")
        tip.setObjectName("tip")
        tip.setWordWrap(True)
        root.addWidget(tip)

        save_btn.clicked.connect(self._on_save_nickname)
        self._tts_check.toggled.connect(self._on_tts)
        self._stt_check.toggled.connect(self._on_stt)
        self._loading = False

    # ------------------------------------------------------------------ 数据
    def load_state(self, health: dict, stt_status: str, tts_status: str, nickname: str) -> None:
        self._loading = True
        self._mode.setText("演示模式（未连接）" if health.get("mock") else "已连接 DeepSeek")
        self._key.setText("已配置 ✓" if health.get("has_api_key") else "未配置 ✗")
        self._model.setText(str(health.get("model", "-")))

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

        self._stt_status.setText(f"{health.get('stt_model', '-')} · {stt_text}")
        self._tts_status.setText(f"{health.get('tts_engine', '-')} · {tts_text}")

        self._tts_check.setChecked(bool(self.settings.value("tts_enabled", True, type=bool)))
        self._stt_check.setChecked(bool(self.settings.value("stt_enabled", True, type=bool)))
        self._nickname.setText(nickname or "")
        self._loading = False

    def _on_tts(self, checked: bool) -> None:
        self.settings.setValue("tts_enabled", checked)
        if not self._loading:
            self.tts_toggled.emit(checked)

    def _on_stt(self, checked: bool) -> None:
        self.settings.setValue("stt_enabled", checked)
        if not self._loading:
            self.stt_toggled.emit(checked)

    def _on_save_nickname(self) -> None:
        self.nickname_saved.emit(self._nickname.text().strip())

    @staticmethod
    def tts_enabled() -> bool:
        return bool(QSettings(ORG, APP).value("tts_enabled", True, type=bool))

    @staticmethod
    def stt_enabled() -> bool:
        return bool(QSettings(ORG, APP).value("stt_enabled", True, type=bool))
