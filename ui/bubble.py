"""头顶气泡：情感标签 + 打字机效果（对齐旧版 app.js 的 showBubble / typeInBubble）。"""
from __future__ import annotations

from PySide6.QtCore import QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPolygonF
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

EMOTION_LABELS = {
    "HAPPY": "开心", "SAD": "难过", "ANGRY": "生气", "SURPRISED": "惊讶",
    "MOTIVATED": "元气", "EMPATHY": "温柔", "NORMAL": "平静",
}
EMOTION_ICONS = {
    "HAPPY": "😄", "SAD": "😢", "ANGRY": "😠", "SURPRISED": "😲",
    "MOTIVATED": "💪", "EMPATHY": "🥰", "NORMAL": "😊",
}

ARROW_H = 9
ARROW_W = 18


class SpeechBubble(QFrame):
    """圆角白色气泡，底部带小三角。内容用打字机逐字显示。"""

    typewriter_finished = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("speechBubble")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            """
            #speechBubble {
                background: rgba(255, 255, 255, 242);
                border-radius: 16px;
            }
            #bubbleContent {
                color: #1f2430;
                font-size: 14px;
                line-height: 150%;
                background: transparent;
            }
            #emotionChip {
                background: rgba(57, 197, 187, 0.16);
                color: #168f86;
                font-size: 11px;
                border-radius: 999px;
                padding: 1px 7px;
            }
            #typingDots {
                color: #39c5bb;
                font-size: 18px;
                letter-spacing: 3px;
                background: transparent;
            }
            """
        )

        self._chip = QLabel(self)
        self._chip.setObjectName("emotionChip")
        self._content = QLabel(self)
        self._content.setObjectName("bubbleContent")
        self._content.setWordWrap(True)
        self._content.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        header.addWidget(self._chip)
        header.addStretch(1)

        body = QVBoxLayout(self)
        body.setContentsMargins(14, 10, 14, 10 + ARROW_H)
        body.setSpacing(4)
        body.addLayout(header)
        body.addWidget(self._content)

        self.setMaximumWidth(320)
        self.setMinimumWidth(120)

        self._full_text = ""
        self._shown = 0
        self._timer = QTimer(self)
        self._timer.setInterval(16)  # 对齐旧版 16ms/字
        self._timer.timeout.connect(self._tick)

        self._autohide = QTimer(self)
        self._autohide.setSingleShot(True)
        self._autohide.timeout.connect(self.hide_bubble)

        self.hide()

    # ------------------------------------------------------------- 绘制小三角
    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 242))
        w = self.width()
        cx = w / 2.0
        poly = QPolygonF([
            QPointF(cx - ARROW_W / 2.0, self.height() - ARROW_H - 1),
            QPointF(cx + ARROW_W / 2.0, self.height() - ARROW_H - 1),
            QPointF(cx, self.height() - 1),
        ])
        path = QPainterPath()
        path.addPolygon(poly)
        painter.drawPath(path)
        painter.end()

    # ------------------------------------------------------------------ 状态
    def show_typing(self, autohide_ms: int = 0) -> None:
        self._timer.stop()
        self._autohide.stop()
        self._chip.hide()
        self._content.setStyleSheet("")
        self._content.setObjectName("typingDots")
        self._content.setText("● ● ●")
        self._full_text = ""
        self.adjustSize()
        self.show()
        self.raise_()
        if autohide_ms:
            self._autohide.start(autohide_ms)

    def show_message(
        self,
        text: str,
        emotion: str = "NORMAL",
        autohide_ms: int = 0,
        typewriter: bool = True,
    ) -> None:
        self._autohide.stop()
        emotion = (emotion or "NORMAL").upper()
        if emotion in EMOTION_LABELS:
            self._chip.setText(f"{EMOTION_ICONS.get(emotion, '')} {EMOTION_LABELS[emotion]}")
            self._chip.show()
        else:
            self._chip.hide()

        self._content.setObjectName("bubbleContent")
        self._content.setStyleSheet("")
        self._full_text = text or ""
        self._shown = 0

        if typewriter and self._full_text:
            self._content.setText("")
            self.adjustSize()
            self.show()
            self.raise_()
            self._timer.start()
        else:
            self._content.setText(self._full_text)
            self.adjustSize()
            self.show()
            self.raise_()
            self.typewriter_finished.emit()
            if autohide_ms:
                self._autohide.start(autohide_ms)

    def _tick(self) -> None:
        if self._shown >= len(self._full_text):
            self._timer.stop()
            self.typewriter_finished.emit()
            return
        self._shown += 1
        self._content.setText(self._full_text[: self._shown])
        self.adjustSize()

    def hide_bubble(self) -> None:
        self._timer.stop()
        self._autohide.stop()
        self.hide()

    @property
    def busy_typing(self) -> bool:
        return self._timer.isActive()

    def stop_typing(self) -> None:
        """跳过打字机，直接显示全文。"""
        if self._timer.isActive():
            self._timer.stop()
            self._shown = len(self._full_text)
            self._content.setText(self._full_text)
            self.adjustSize()
            self.typewriter_finished.emit()
