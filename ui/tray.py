"""系统托盘：显示/隐藏、控制台、设置、完全退出（网页版没有的能力）。"""
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
    """封装 QSystemTrayIcon 与右键菜单。

    菜单里刻意把「隐藏 Miku」和「完全退出」分开写清楚：
    桌宠窗口是无边框、不进任务栏的，用户没法像普通程序那样从任务栏把它找回来，
    所以这两个动作必须一眼能分辨 —— 混淆的后果就是「关了窗口但进程还在」。
    """

    def __init__(
        self,
        window,
        on_settings,
        on_quit,
        on_console=None,
        on_visibility=None,
    ) -> None:
        self.icon = QSystemTrayIcon(make_tray_icon(), window)
        self.icon.setToolTip("MikuAgent · 初音未来桌宠")

        menu = QMenu()
        self.act_toggle = QAction("隐藏 Miku", menu)
        act_console = QAction("打开控制台…", menu)
        act_settings = QAction("设置…", menu)
        act_center = QAction("回到屏幕中央", menu)
        act_quit = QAction("完全退出", menu)
        act_quit.setToolTip("停掉语音、松开摄像头、释放显存并退出整个进程")

        self.act_toggle.triggered.connect(self._toggle)
        act_console.triggered.connect(on_console or (lambda: None))
        act_settings.triggered.connect(on_settings)
        act_center.triggered.connect(lambda: window.center_on_screen())
        act_quit.triggered.connect(on_quit)

        menu.addAction(act_console)
        menu.addAction(self.act_toggle)
        menu.addAction(act_center)
        menu.addSeparator()
        menu.addAction(act_settings)
        menu.addSeparator()
        menu.addAction(act_quit)

        self.icon.setContextMenu(menu)
        self.icon.activated.connect(self._on_activated)
        self._window = window
        self._on_visibility = on_visibility

    def _on_activated(self, reason) -> None:
        from PySide6.QtWidgets import QSystemTrayIcon as T

        if reason == T.ActivationReason.Trigger:  # 单击托盘图标切换显示
            self._toggle()

    def _toggle(self) -> None:
        self.set_visible(not self._window.isVisible())

    def set_visible(self, visible: bool) -> None:
        if visible:
            self._window.show()
            self._window.raise_()
            self.act_toggle.setText("隐藏 Miku")
        else:
            self._window.hide()
            self.act_toggle.setText("显示 Miku")
        if self._on_visibility is not None:
            self._on_visibility(bool(visible))

    def show(self) -> None:
        self.icon.show()

    def hide(self) -> None:
        self.icon.hide()
