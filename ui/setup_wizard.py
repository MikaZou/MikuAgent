"""首次启动设置向导。

目标：新用户第一次跑起来时，不要面对一个「不知道用哪个引擎」的黑箱，
而是明确告诉他每个选项的**资源代价**，让他自己选。

选择结果写入 `.env`（就地改 key、保留注释），这样：
  * 用户随时能打开 .env 看到自己的选择
  * 后续启动无需再问
  * 与现有 config 读取路径完全一致，不需要第二套配置层

「本地 vs 云端」是本项目最关键的取舍，所以向导里对每项都标了
显存/内存/联网要求 —— 这些数字来自 docs/TECHNICAL.md 的实测。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
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

from backend import envfile

QSS = """
QDialog { background: #f7f9fa; }
QLabel { color: #1f2430; font-size: 13px; }
QLabel#h1 { font-size: 19px; font-weight: 700; color: #0f766e; }
QLabel#h2 { font-size: 14px; font-weight: 700; color: #1f2430; }
QLabel#tip { color: #6b7280; font-size: 12px; }
QLabel#cost { color: #b45309; font-size: 12px; }
QFrame#card {
    background: #ffffff; border: 1px solid #e3e8ea; border-radius: 10px;
}
QRadioButton { font-size: 13px; padding: 3px 0; }
QLineEdit {
    border: 1px solid #cbd5e1; border-radius: 8px; padding: 7px 10px;
    font-size: 13px; background: #ffffff;
}
QPushButton {
    background: #e5eaec; color: #1f2430; border: none; border-radius: 9px;
    padding: 10px 18px; font-size: 14px;
}
QPushButton:hover { background: #d7dee1; }
QPushButton#primary {
    background: #39c5bb; color: #fff; border: none; border-radius: 9px;
    padding: 10px 22px; font-size: 14px; font-weight: 600;
}
QPushButton#primary:hover { background: #2fb3a9; }
"""


class Option(QRadioButton):
    """带资源代价说明的单选项。"""

    def __init__(self, label: str, cost: str, value: str, parent=None) -> None:
        super().__init__(label, parent)
        self.value = value
        self.cost = cost


class SetupWizard(QDialog):
    """首次启动向导：让用户选本地还是云端。"""

    done = Signal(dict)

    TTS_OPTIONS = [
        ("云端 MiniMax（推荐）", "不占显存 · 不吃本地内存 · 每次启动省约 3.3GB · 需联网与按量计费", "minimax"),
        ("本地 GPT-SoVITS", "约 1.4~2.2GB 显存 + 2.9GB 内存 · 需 NVIDIA 显卡 · 离线可用", "sovits"),
        ("微软在线 edge", "几乎不占资源 · 无初音音色 · 需联网", "edge"),
        ("关闭语音输出", "Miku 只用文字回复", "none"),
    ]
    STT_OPTIONS = [
        ("云端 MiniMax ASR", "几乎不吃本地内存 · 需联网", "minimax"),
        ("本地 Whisper", "约 1GB 内存 · 离线可用", "local-whisper"),
    ]

    def __init__(self, parent=None, edit_mode: bool = False) -> None:
        super().__init__(parent)
        self.edit_mode = edit_mode
        self.setWindowTitle("MikuAgent 设置" if edit_mode else "MikuAgent 首次设置")
        self.setMinimumWidth(520)
        self.setStyleSheet(QSS)

        cur = envfile.read_env()

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(12)

        title = QLabel("修改设置" if edit_mode else "欢迎使用 MikuAgent ♪")
        title.setObjectName("h1")
        root.addWidget(title)
        sub = QLabel(
            "改完点「保存并应用」会**立即生效**，不需要重启："
            "从云端切到本地会自动拉起 GPT-SoVITS 服务（首次约 15 秒），"
            "从本地切到云端会自动停掉它并把显存释放掉。"
            if edit_mode
            else
            "第一次运行需要填一下密钥、选一下「语音怎么走」。选错了也没关系，"
            "随时可以改 .env 或在设置面板里调整。"
        )
        sub.setObjectName("tip")
        sub.setWordWrap(True)
        root.addWidget(sub)

        # ---------------- 必填：DeepSeek ----------------
        root.addWidget(self._heading("DeepSeek 大脑（必填）"))
        ds_row = QHBoxLayout()
        ds_row.addWidget(QLabel("API Key"))
        self.ds_key = QLineEdit(cur.get("DEEPSEEK_API_KEY", ""))
        self.ds_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.ds_key.setPlaceholderText("sk-…（在 platform.deepseek.com 申请）")
        ds_row.addWidget(self.ds_key, 1)
        root.addLayout(ds_row)
        ds_tip = QLabel(
            "不填也能启动，但 Miku 只会用本地预设回复（离线演示模式），不会真的对话。"
        )
        ds_tip.setObjectName("tip")
        ds_tip.setWordWrap(True)
        root.addWidget(ds_tip)

        # ---------------- 称呼 ----------------
        nick_row = QHBoxLayout()
        nick_row.addWidget(QLabel("怎么称呼你"))
        self.nickname = QLineEdit(cur.get("USER_NICKNAME", ""))
        self.nickname.setPlaceholderText("例如：主人 / 小憷（可留空）")
        nick_row.addWidget(self.nickname, 1)
        root.addLayout(nick_row)

        # ---------------- TTS ----------------
        root.addWidget(self._heading("语音输出（Miku 说话）"))
        self.tts_group = QButtonGroup(self)
        for label, cost, value in self.TTS_OPTIONS:
            root.addWidget(self._card(Option(label, cost, value), self.tts_group, value,
                                      cur.get("TTS_ENGINE", "minimax")))

        key_row = QHBoxLayout()
        key_row.addWidget(QLabel("MiniMax Key"))
        self.api_key = QLineEdit(cur.get("MINIMAX_API_KEY", ""))
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("选云端时填，形如 sk-api-…（也可稍后再填）")
        key_row.addWidget(self.api_key, 1)
        root.addLayout(key_row)

        # ---------------- STT ----------------
        root.addWidget(self._heading("语音输入（按住说话）转写"))
        self.stt_group = QButtonGroup(self)
        for label, cost, value in self.STT_OPTIONS:
            root.addWidget(self._card(Option(label, cost, value), self.stt_group, value,
                                      cur.get("STT_TRANSCRIBER", "minimax")))

        # ---------------- 视频对话 ----------------
        self.video = QCheckBox("开启视频对话（让 Miku 通过摄像头看到你）")
        self.video.setChecked(cur.get("VISION_ENABLED", "false").lower() in ("1", "true", "yes", "on"))
        root.addWidget(self.video)
        vtip = QLabel("画面会在你说完话的那一刻抓一帧上传到云端识别；不写入本地磁盘。")
        vtip.setObjectName("tip")
        vtip.setWordWrap(True)
        root.addWidget(vtip)

        # ---------------- 手机端 ----------------
        self.remote = QCheckBox("开启手机端（同一 WiFi 下用手机浏览器访问）")
        self.remote.setChecked(cur.get("REMOTE_ENABLED", "false").lower() in ("1", "true", "yes", "on"))
        root.addWidget(self.remote)
        rtip = QLabel(
            "⚠️ 会在本机开一个监听所有网卡的端口，且**没有密码** —— "
            "同一局域网内的设备都能连上来对话，会消耗你的 API 额度。"
            "只在你自己的网络里开。"
        )
        rtip.setObjectName("cost")
        rtip.setWordWrap(True)
        root.addWidget(rtip)

        # ---------------- 按钮 ----------------
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        if edit_mode:
            cancel = QPushButton("取消")
            cancel.setCursor(Qt.CursorShape.PointingHandCursor)
            cancel.clicked.connect(self.reject)
            btn_row.addWidget(cancel)
        btn = QPushButton("保存并应用" if edit_mode else "开始使用")
        btn.setObjectName("primary")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(self.accept)
        btn_row.addWidget(btn)
        root.addLayout(btn_row)

        self.tts_group.buttonClicked.connect(self._sync_key_enabled)
        self._sync_key_enabled()

    # ------------------------------------------------------------ 小组件
    @staticmethod
    def _heading(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("h2")
        return lbl

    def _card(self, option: Option, group: QButtonGroup, value: str, default: str) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        box = QVBoxLayout(card)
        box.setContentsMargins(12, 9, 12, 9)
        box.setSpacing(2)
        box.addWidget(option)
        cost = QLabel(option.cost)
        cost.setObjectName("cost")
        cost.setWordWrap(True)
        box.addWidget(cost)
        group.addButton(option)
        if value == default:
            option.setChecked(True)
        return card

    def _selected(self, group: QButtonGroup) -> str:
        btn = group.checkedButton()
        return getattr(btn, "value", "")

    def _sync_key_enabled(self) -> None:
        needs_key = self._selected(self.tts_group) == "minimax" or \
            self._selected(self.stt_group) == "minimax"
        self.api_key.setEnabled(needs_key)

    # ------------------------------------------------------------ 提交
    def choices(self) -> dict:
        return {
            "DEEPSEEK_API_KEY": self.ds_key.text().strip(),
            "TTS_ENGINE": self._selected(self.tts_group),
            "STT_TRANSCRIBER": self._selected(self.stt_group),
            "MINIMAX_API_KEY": self.api_key.text().strip(),
            "VISION_ENABLED": "true" if self.video.isChecked() else "false",
            "REMOTE_ENABLED": "true" if self.remote.isChecked() else "false",
        }

    def apply(self) -> list[str]:
        """把选择写进 .env，返回被修改的 key。"""
        values = self.choices()
        # 空 Key 不要覆盖掉已有的（用户可能只是没改这一栏）
        for k in ("DEEPSEEK_API_KEY", "MINIMAX_API_KEY"):
            if not values.get(k):
                values.pop(k)
        return envfile.update_env(values)
