"""系统托盘：显示/隐藏、设置、退出（网页版没有的能力）。"""
from __future__ import annotations

from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon


def make_tray_icon() -> QIcon:
    """没有 ico 资源时，用代码画一个初音色圆点，避免依赖外部文件。"""
    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setBrush(QColor(57, 197, 187))
    painter.setPen(QColor(20, 140, 132))
    painter.drawEllipse(6, 6, 52, 52)
    painter.setBrush(QColor(255, 255, 255))
    painter.setPen(QColor(0, 0, 0, 0))
    painter.drawEllipse(20, 26, 9, 12)
    painter.drawEllipse(35, 26, 9, 12)
    painter.end()
    return QIcon(pixmap)


class Tray:
    """封装 QSystemTrayIcon 与右键菜单。"""

    def __init__(self, window, on_settings, on_quit) -> None:
        self.icon = QSystemTrayIcon(make_tray_icon(), window)
        self.icon.setToolTip("MikuAgent · 初音未来桌宠")

        menu = QMenu()
        self.act_toggle = QAction("隐藏 Miku", menu)
        act_settings = QAction("设置…", menu)
        act_center = QAction("回到屏幕中央", menu)
        act_quit = QAction("退出", menu)

        self.act_toggle.triggered.connect(self._toggle)
        act_settings.triggered.connect(on_settings)
        act_center.triggered.connect(lambda: window.center_on_screen())
        act_quit.triggered.connect(on_quit)

        menu.addAction(self.act_toggle)
        menu.addAction(act_center)
        menu.addSeparator()
        menu.addAction(act_settings)
        menu.addSeparator()
        menu.addAction(act_quit)

        self.icon.setContextMenu(menu)
        self.icon.activated.connect(self._on_activated)
        self._window = window

    def _on_activated(self, reason) -> None:
        from PySide6.QtWidgets import QSystemTrayIcon as T

        if reason == T.ActivationReason.Trigger:  # 单击托盘图标切换显示
            self._toggle()

    def _toggle(self) -> None:
        if self._window.isVisible():
            self._window.hide()
            self.act_toggle.setText("显示 Miku")
        else:
            self._window.show()
            self._window.raise_()
            self.act_toggle.setText("隐藏 Miku")

    def show(self) -> None:
        self.icon.show()

    def hide(self) -> None:
        self.icon.hide()
