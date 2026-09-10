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
                padding: 9px 18px;
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
                padding: 6px 10px;
                font-size: 16px;
                background: rgba(57, 197, 187, 0.14);
            }
            #micButton:hover { background: rgba(57, 197, 187, 0.28); }
            #micButton[recording="true"] {
                background: #ef4444;
                color: #ffffff;
            }
            """
        )

        self.input = ChatLineEdit(self)
        self.input.setObjectName("chatInput")
        self.input.setPlaceholderText(DEFAULT_PLACEHOLDER)
        self.input.setMaxLength(2000)
        self.input.submitted.connect(self.submitted.emit)

        self.mic = MicButton(self)
        self.mic.setObjectName("micButton")
        self.mic.hold_started.connect(self._on_hold_started)
        self.mic.hold_finished.connect(self._on_hold_finished)
        self.mic.hold_cancelled.connect(self._on_hold_cancelled)

        self.send = QPushButton("发送", self)
        self.send.setObjectName("sendButton")
        self.send.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send.clicked.connect(self._on_send)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        layout.addWidget(self.input, 1)
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
    def set_recording(self, on: bool) -> None:
        self.mic.setProperty("recording", "true" if on else "false")
        self.mic.style().unpolish(self.mic)
        self.mic.style().polish(self.mic)
        self.input.setPlaceholderText(LISTENING_PLACEHOLDER if on else DEFAULT_PLACEHOLDER)

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
