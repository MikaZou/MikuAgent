"""底部输入栏：文本框 + 发送 + 按住说话。

可见性沿用网页版修好后的那套逻辑：
  - 鼠标悬停在桌宠上 → 显示
  - 输入框有焦点、或里面还有没发出去的草稿 → 钉住不隐藏
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QToolButton,
)

DEFAULT_PLACEHOLDER = "和 Miku 说点什么吧…"
LISTENING_PLACEHOLDER = "聆听中…松开结束"
ATTACHED_PLACEHOLDER = "📎 已配好一张图，发送时一起给 Miku 看"

# 图标按钮固定尺寸。QToolButton 默认会把 emoji 按钮撑到 44px 以上，
# 输入栏总宽只有 336px，四个按钮一排会把输入框挤到 105px（太窄）。
ICON_BTN_SIZE = 32
LAYOUT_SPACING = 5


class ChatLineEdit(QLineEdit):
    """回车发送，但输入法组字过程中的回车不发送。"""

    submitted = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._preedit = ""

    def inputMethodEvent(self, event) -> None:  # noqa: N802
        self._preedit = event.preeditString() or ""
        super().inputMethodEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        is_enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        if is_enter and not self._preedit:
            text = self.text().strip()
            if text:
                self.submitted.emit(text)
            return
        super().keyPressEvent(event)


class MicButton(QToolButton):
    """按住说话按钮。"""

    hold_started = Signal()
    hold_finished = Signal()
    hold_cancelled = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setText("🎤")
        self.setToolTip("按住说话")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._held = False

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._held = True
            self.hold_started.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._held and event.button() == Qt.MouseButton.LeftButton:
            self._held = False
            if self.rect().contains(event.position().toPoint()):
                self.hold_finished.emit()
            else:
                self.hold_cancelled.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def cancel_hold(self) -> None:
        self._held = False


class InputBar(QFrame):
    """输入栏容器。"""

    submitted = Signal(str)
    hold_started = Signal()
    hold_finished = Signal()
    hold_cancelled = Signal()
    state_changed = Signal()
    video_toggled = Signal(bool)
    screen_requested = Signal()
    clear_image_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("inputBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            """
            #inputBar {
                background: rgba(255, 255, 255, 240);
                border-radius: 16px;
            }
            #chatInput {
                border: 1px solid rgba(0, 0, 0, 30);
                border-radius: 11px;
                padding: 9px 13px;
                font-size: 14px;
                background: #ffffff;
                color: #1f2430;
            }
            #chatInput:focus {
                border: 1px solid #39c5bb;
            }
            #sendButton {
                border: none;
                border-radius: 11px;
                padding: 9px 12px;
                font-size: 14px;
                font-weight: 600;
                color: #ffffff;
                background: #39c5bb;
            }
            #sendButton:hover { background: #2fb3a9; }
            #sendButton:disabled { background: #a9dcd8; }
            #micButton {
                border: none;
                border-radius: 11px;
                padding: 6px 6px;
                font-size: 16px;
                background: rgba(57, 197, 187, 0.14);
            }
            #micButton:hover { background: rgba(57, 197, 187, 0.28); }
            #micButton[recording="true"] {
                background: #ef4444;
                color: #ffffff;
            }
            #videoButton, #screenButton {
                border: none;
                border-radius: 11px;
                padding: 5px 6px;
                font-size: 15px;
                background: rgba(57, 197, 187, 0.14);
            }
            #videoButton:hover, #screenButton:hover {
                background: rgba(57, 197, 187, 0.28);
            }
            #videoButton[video="true"] {
                background: #ef4444;
                color: #ffffff;
            }
            #videoButton[attached="true"], #screenButton[attached="true"] {
                background: #39c5bb;
                color: #ffffff;
            }
            """
        )

        self._recording = False
        self._attached = False
        self._screen_allowed = True

        self.input = ChatLineEdit(self)
        self.input.setObjectName("chatInput")
        self.input.setPlaceholderText(DEFAULT_PLACEHOLDER)
        self.input.setMaxLength(2000)
        self.input.submitted.connect(self.submitted.emit)

        self.mic = MicButton(self)
        self.mic.setObjectName("micButton")
        self.mic.setFixedSize(ICON_BTN_SIZE, ICON_BTN_SIZE)
        self.mic.hold_started.connect(self._on_hold_started)
        self.mic.hold_finished.connect(self._on_hold_finished)
        self.mic.hold_cancelled.connect(self._on_hold_cancelled)

        # 视频对话：开着的时候每秒抓几次画面，说话结束时自动配一张给 Miku 看
        self.video = QToolButton(self)
        self.video.setObjectName("videoButton")
        self.video.setText("📹")
        self.video.setCheckable(True)
        self.video.setFixedSize(ICON_BTN_SIZE, ICON_BTN_SIZE)
        self.video.setCursor(Qt.CursorShape.PointingHandCursor)
        self.video.setToolTip("视频对话：让 Miku 在每轮语音里看见你")
        self.video.toggled.connect(self.video_toggled.emit)

        # 截屏发给 Miku（没有摄像头时也能用；视频模式开着时让位给它）
        self.screen = QToolButton(self)
        self.screen.setObjectName("screenButton")
        self.screen.setText("🖥️")
        self.screen.setFixedSize(ICON_BTN_SIZE, ICON_BTN_SIZE)
        self.screen.setCursor(Qt.CursorShape.PointingHandCursor)
        self.screen.setToolTip("把当前屏幕截图发给 Miku")
        self.screen.clicked.connect(self.screen_requested.emit)

        self.send = QPushButton("发送", self)
        self.send.setObjectName("sendButton")
        self.send.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send.clicked.connect(self._on_send)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(LAYOUT_SPACING)
        layout.addWidget(self.input, 1)
        layout.addWidget(self.screen)
        layout.addWidget(self.video)
        layout.addWidget(self.mic)
        layout.addWidget(self.send)

        self.input.textChanged.connect(lambda _: self.state_changed.emit())
        self.input.installEventFilter(self)

    # ------------------------------------------------------------------ 事件
    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self.input and event.type() in (
            event.Type.FocusIn,
            event.Type.FocusOut,
        ):
            self.state_changed.emit()
        return super().eventFilter(obj, event)

    def _on_send(self) -> None:
        text = self.input.text().strip()
        if text:
            self.submitted.emit(text)

    def _on_hold_started(self) -> None:
        self.set_recording(True)
        self.hold_started.emit()

    def _on_hold_finished(self) -> None:
        self.set_recording(False)
        self.hold_finished.emit()

    def _on_hold_cancelled(self) -> None:
        self.set_recording(False)
        self.hold_cancelled.emit()

    # ------------------------------------------------------------------ 状态
    @staticmethod
    def _restyle(widget) -> None:
        """改过动态属性（如 recording/video）之后要重新上样式。"""
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _refresh_placeholder(self) -> None:
        if self._recording:
            self.input.setPlaceholderText(LISTENING_PLACEHOLDER)
        elif self._attached:
            self.input.setPlaceholderText(ATTACHED_PLACEHOLDER)
        else:
            self.input.setPlaceholderText(DEFAULT_PLACEHOLDER)

    def set_recording(self, on: bool) -> None:
        self._recording = bool(on)
        self.mic.setProperty("recording", "true" if on else "false")
        self._restyle(self.mic)
        self._refresh_placeholder()

    def set_video_enabled(self, on: bool) -> None:
        """同步视频按钮状态（blockSignals 避免与上层形成回环）。"""
        self.video.blockSignals(True)
        self.video.setChecked(bool(on))
        self.video.blockSignals(False)
        self.video.setProperty("video", "true" if on else "false")
        self._restyle(self.video)
        # 视频模式开着时画面来自摄像头，把截屏按钮收起来省地方
        self.screen.setVisible(self._screen_allowed and not on)

    def set_video_visible(self, visible: bool, screen_allowed: bool = True) -> None:
        """摄像头可用时才显示 📹；没有摄像头则退化为只有 🖥️ 截屏。"""
        self._screen_allowed = bool(screen_allowed)
        self.video.setVisible(visible)
        self.screen.setVisible(self._screen_allowed and not self.video.isChecked())

    def set_attached(self, has_image: bool, source: str = "camera") -> None:
        """标记「已经配好一张图，下次发送会带上」。"""
        self._attached = bool(has_image)
        for widget in (self.video, self.screen):
            widget.setProperty("attached", "false")
            self._restyle(widget)
        if has_image:
            target = self.screen if source == "screen" else self.video
            target.setProperty("attached", "true")
            self._restyle(target)
        self._refresh_placeholder()

    def set_busy(self, busy: bool) -> None:
        self.send.setEnabled(not busy)
        self.input.setEnabled(not busy)

    def set_mic_visible(self, visible: bool) -> None:
        self.mic.setVisible(visible)

    def take_text(self) -> str:
        text = self.input.text().strip()
        self.input.clear()
        return text

    def set_text(self, text: str) -> None:
        self.input.setText(text)
        self.input.setFocus()

    def focus_input(self) -> None:
        self.input.setFocus()

    # 有草稿或正在输入 → 输入栏必须钉住不隐藏
    @property
    def wants_visible(self) -> bool:
        return bool(self.input.text().strip()) or self.input.hasFocus()
