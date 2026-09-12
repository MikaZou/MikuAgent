"""头顶气泡：情感标签 + 打字机效果（对齐旧版 app.js 的 showBubble / typeInBubble）。"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPolygonF
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

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
    # 气泡显示/隐藏时发出，供外层重新排布角标按钮（避免遮挡、也避免留白）
    visibility_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("speechBubble")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            """
            #speechBubble {
                background: rgba(255, 255, 255, 252);
                border-radius: 16px;
                border: 1px solid rgba(57, 197, 187, 0.35);
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
            /* 内容区可滚动：长回复超出气泡高度时，用户可以上下滑动看全文 */
            #bubbleScroll, #bubbleScroll > QWidget > QWidget {
                background: transparent;
                border: none;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 6px;
                margin: 0px;
            }
            QScrollBar::handle:vertical {
                background: rgba(57, 197, 187, 0.45);
                border-radius: 3px;
                min-height: 18px;
            }
            QScrollBar::handle:vertical:hover {
                background: rgba(57, 197, 187, 0.75);
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: transparent;
            }
            """
        )

        self._chip = QLabel(self)
        self._chip.setObjectName("emotionChip")
        self._content = QLabel()
        self._content.setObjectName("bubbleContent")
        self._content.setWordWrap(True)
        self._content.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        # 必须显式顶对齐：QLabel 默认垂直居中，而在滚动区里它会被拉得比内容高，
        # 结果整段文字被顶到可视区下半部分，上面留一大片空白（实测过）。
        self._content.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        # 纵向 Minimum：让它贴合内容高度，不要被滚动区拉伸
        self._content.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
        )

        # 内容用滚动区包起来：长回复超出气泡最大高度时可以上下滑动看完，
        # 而不是被硬裁掉（之前就是直接裁，末尾几个字永远看不到）。
        self._scroll = QScrollArea(self)
        self._scroll.setObjectName("bubbleScroll")
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._content)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.viewport().setAutoFillBackground(False)
        self._scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        header.addWidget(self._chip)
        header.addStretch(1)

        body = QVBoxLayout(self)
        body.setContentsMargins(14, 10, 14, 10 + ARROW_H)
        body.setSpacing(4)
        body.addLayout(header)
        body.addWidget(self._scroll, 1)

        # 宽度由 PetWindow._layout_children 统一设定；这里只兜底一个最小宽度，
        # 不再设 maximumWidth —— 否则短句气泡会被压得很小。
        self.setMinimumWidth(140)

        self._full_text = ""
        self._shown = 0
        self._timer = QTimer(self)
        self._timer.setInterval(16)  # 对齐旧版 16ms/字
        self._timer.timeout.connect(self._tick)

        self._autohide = QTimer(self)
        self._autohide.setSingleShot(True)
        self._autohide.timeout.connect(self.hide_bubble)

        # 自动跟随到底部；用户一旦自己往上滚，就停在他看的位置不要拽回来
        self._follow = True
        self._auto_scrolling = False
        # 气泡高度上限，由 PetWindow 通过 set_max_height 告知（0 = 不限）
        self._max_height = 0
        self._scroll.verticalScrollBar().valueChanged.connect(self._on_scrolled)

        self.hide()

    # ------------------------------------------------------------- 滚动 / 高度
    @property
    def _bar(self):
        return self._scroll.verticalScrollBar()

    def content_width(self) -> int:
        """内容可用宽度（扣掉左右内边距）。"""
        m = self.layout().contentsMargins()
        return max(1, self.width() - m.left() - m.right())

    def measure_content_height(self) -> int:
        """文本在当前宽度下需要多高。

        不用 QLabel.heightForWidth()：实测它会明显**高估**（320px 宽、
        实际 4 行的文本算成 216px，真实只要约 84px），结果是往下滚
        能看到一大片空白。改用 QFontMetrics.boundingRect 按实际宽度
        和换行规则算，和 QLabel 的渲染口径一致。

        注意这里返回的是**精确**高度，不要加余量 —— 余量会让内容比
        可视区高一点点，短文本也会冒出一条多余的滚动条。防裁切的余量
        加在气泡总高度上（见 apply_content_height）。
        """
        text = self._content.text()
        if not text:
            return 0
        fm = self._content.fontMetrics()
        rect = fm.boundingRect(
            QRect(0, 0, self.content_width(), 100000),
            int(Qt.TextFlag.TextWordWrap),
            text,
        )
        return max(rect.height(), fm.height())

    def _chrome_height(self) -> int:
        """气泡里除正文之外的固定开销：内边距 + 情绪 chip + 间距 + 小三角。"""
        m = self.layout().contentsMargins()
        chip = self._chip.sizeHint().height() if self._chip.isVisible() else 0
        return m.top() + m.bottom() + chip + self.layout().spacing()

    def _pad(self) -> int:
        """防最后一行被裁的余量，只算进气泡高度、不算进内容高度。"""
        return max(2, self._content.fontMetrics().lineSpacing() // 4)

    def set_max_height(self, height: int) -> None:
        self._max_height = int(height)
        self.apply_content_height()

    def apply_content_height(self) -> None:
        """按内容把气泡撑到合适高度（上限 _max_height）。

        引入 QScrollArea 之后 ``adjustSize()`` 不再管用了：滚动区的 sizeHint
        不随内容增长，气泡会缩到最小、下面留一大片空白然后全靠滚动。
        所以高度必须自己算：min(内容 + chrome + 余量, 上限)。
        """
        needed = self.measure_content_height()
        if needed and needed != self._content.minimumHeight():
            self._content.setMinimumHeight(needed)
        if self._max_height <= 0:
            return
        want = min(self._chrome_height() + needed + self._pad(), self._max_height)
        if want > 0 and self.height() != want:
            self.setFixedHeight(want)

    def _on_scrolled(self, value: int) -> None:
        """区分「用户滚动」和「我们自动跟随」。"""
        if self._auto_scrolling:
            return
        self._follow = value >= self._bar.maximum() - 4

    def _scroll_to_bottom(self) -> None:
        self._auto_scrolling = True
        self._bar.setValue(self._bar.maximum())
        self._auto_scrolling = False

    def _after_content_change(self) -> None:
        """内容变了：重算气泡高度，需要的话跟到底部。

        要等布局跑完才算得准，所以延到下一轮事件循环。
        """
        QTimer.singleShot(0, self.apply_content_height)
        if self._follow:
            QTimer.singleShot(0, self._scroll_to_bottom)

    def scroll_to_top(self) -> None:
        self._follow = False
        self._bar.setValue(0)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # 宽度变了要重排文本，但不能再改高度（否则和 apply_content_height 打架）
        QTimer.singleShot(0, self._reflow_content)

    def _reflow_content(self) -> None:
        needed = self.measure_content_height()
        if needed and needed != self._content.minimumHeight():
            self._content.setMinimumHeight(needed)

    # ------------------------------------------------------------- 绘制小三角
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self.visibility_changed.emit(False)

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
        self.scroll_to_top()
        self._follow = True
        self._after_content_change()
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
            self.apply_content_height()
            self.show()
            self.raise_()
            self.scroll_to_top()
            self._follow = True
            self._timer.start()
        else:
            self._content.setText(self._full_text)
            self.apply_content_height()
            self.show()
            self.raise_()
            self.scroll_to_top()
            self._follow = False   # 整段直接显示时不自动滚，让用户从头读
            self._after_content_change()
            self.typewriter_finished.emit()
            if autohide_ms:
                self._autohide.start(autohide_ms)

    def _snap_top_if_overflow(self) -> None:
        """打完字后如果内容超框，回到顶部。

        打字过程中是跟随底部的（这样能看到字在往上冒），但回复完整之后
        应该从第一句开始读 —— 停在底部会让用户以为「只有这几句」。
        用户中途自己滚过（_follow 已为 False）就不动他。
        """
        self.apply_content_height()
        if self._follow and self._bar.maximum() > 0:
            self.scroll_to_top()

    def _tick(self) -> None:
        if self._shown >= len(self._full_text):
            self._timer.stop()
            self.typewriter_finished.emit()
            QTimer.singleShot(0, self._snap_top_if_overflow)
            return
        self._shown += 1
        self._content.setText(self._full_text[: self._shown])
        self._after_content_change()

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
            self._after_content_change()
            QTimer.singleShot(0, self._snap_top_if_overflow)
            self.typewriter_finished.emit()
