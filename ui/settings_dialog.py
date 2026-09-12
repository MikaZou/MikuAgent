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

import config

ORG = "MikuAgent"
APP = "MikuAgent"


class SettingsDialog(QDialog):
    """运行状态 + 语音开关 + 昵称。"""

    nickname_saved = Signal(str)
    tts_toggled = Signal(bool)
    stt_toggled = Signal(bool)
    video_toggled = Signal(bool)
    # 请求打开「首次设置向导」的编辑模式（改引擎 / API Key / 手机端等）
    reconfigure_requested = Signal()

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
            QPushButton#ghost {
                background: #ffffff; color: #0f766e;
                border: 1px solid #99e0da; font-weight: 600;
                padding: 7px 12px;
            }
            QPushButton#ghost:hover { background: #eafaf8; }
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
        self._video_check = QCheckBox("视频对话（让 Miku 在每轮语音里看见你）")
        self._nickname = QLineEdit()
        self._nickname.setPlaceholderText("例如：主人 / 小名")
        save_btn = QPushButton("保存称呼")

        # 改引擎 / API Key / 手机端这些「首次设置」项，都从这一个入口进，
        # 避免设置面板和向导两套 UI 各改一半、状态对不上。
        reconf_btn = QPushButton("⚙ 修改配置")
        reconf_btn.setObjectName("ghost")
        reconf_btn.setToolTip(
            "重新打开首次设置向导：语音引擎（本地/云端）、API Key、"
            "视频对话、手机端。保存后立即生效，无需重启。"
        )
        reconf_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        form = QFormLayout()
        form.addRow("运行模式", self._mode)
        form.addRow("API Key", self._key)
        form.addRow("模型", self._model)
        form.addRow("语音输出引擎", self._tts_status)
        form.addRow("语音输入引擎", self._stt_status)
        form.addRow("", reconf_btn)

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
        root.addWidget(self._video_check)

        vision_tip = QLabel(
            "📷 开启后摄像头会保持打开（指示灯常亮），但只在你说完话的那一刻抓一帧上传给 DeepSeek，"
            "不是持续上传。画面不写入本地磁盘。"
        )
        vision_tip.setObjectName("tip")
        vision_tip.setWordWrap(True)
        root.addWidget(vision_tip)

        nick_row = QHBoxLayout()
        nick_row.addWidget(QLabel("称呼"))
        nick_row.addWidget(self._nickname, 1)
        nick_row.addWidget(save_btn)
        root.addLayout(nick_row)

        tip = QLabel(
            "💡 换引擎、改 API Key、开关手机端，都点上面的「修改配置」，保存后立即生效、不用重启。"
            "切到本地 GPT-SoVITS 会在后台加载（首次约 15 秒），加载期间 Miku 暂时没有声音。"
        )
        tip.setObjectName("tip")
        tip.setWordWrap(True)
        root.addWidget(tip)

        save_btn.clicked.connect(self._on_save_nickname)
        reconf_btn.clicked.connect(self.reconfigure_requested.emit)
        self._tts_check.toggled.connect(self._on_tts)
        self._stt_check.toggled.connect(self._on_stt)
        self._video_check.toggled.connect(self._on_video)
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
        self._video_check.setChecked(
            bool(self.settings.value("video_enabled", config.VISION_ENABLED, type=bool))
        )
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

    def _on_video(self, checked: bool) -> None:
        self.settings.setValue("video_enabled", checked)
        if not self._loading:
            self.video_toggled.emit(checked)

    def _on_save_nickname(self) -> None:
        self.nickname_saved.emit(self._nickname.text().strip())

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
    def setup_done() -> bool:
        """是否已经跑过首次设置向导。"""
        return bool(QSettings(ORG, APP).value("setup_done", False, type=bool))

    @staticmethod
    def mark_setup_done() -> None:
        QSettings(ORG, APP).setValue("setup_done", True)
