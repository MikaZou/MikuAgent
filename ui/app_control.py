"""集中式「完全退出」。

为什么单独一层：

退出的入口有三个 —— 桌宠右上角的 ×、托盘菜单、新控制台上的按钮。以前每个入口
各自写一半，而且都只是调一次 `QApplication.quit()`。只要有任何一处没完全松开
（还活着的摄像头线程、没销毁的 Live2D 渲染器、占着显存的 TTS 合成进程），
进程就会留在后台，用户看到的现象就是「窗口关了，但应用没关」。

所以把「关掉一切」收敛成一个函数，并且配一个**独立线程的看门狗**：
Qt 事件循环只要被谁卡住，QTimer 也不会再触发 —— 必须用 threading.Timer，
N 秒后无条件 `os._exit(0)`，保证「完全关闭」这个承诺一定兑现。
"""
from __future__ import annotations

import os
import threading
from typing import Optional

from PySide6.QtCore import QObject, QTimer

# 看门狗时限：正常退出路径（停远程服务 6s 上限 + 收 TTS + dispose）实测 1~2 秒，
# 8 秒足够宽裕；真卡住了也不会让用户等太久。
WATCHDOG_SECONDS = 8.0


class AppControl(QObject):
    """持有各组件引用，提供唯一的 `quit_all()`。"""

    def __init__(
        self,
        app,
        window=None,
        console=None,
        tray=None,
        remote=None,
        tts=None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.app = app
        self.window = window
        self.console = console
        self.tray = tray
        self.remote = remote
        self.tts = tts
        self._quitting = False
        self._watchdog: Optional[threading.Timer] = None

    # ------------------------------------------------------------------ 退出
    def quit_all(self, reason: str = "") -> None:
        """关掉一切并退出进程。可被重复调用（第二次起直接忽略）。"""
        if self._quitting:
            return
        self._quitting = True
        print(f"[Exit] 完全退出开始{('（' + reason + '）') if reason else ''}", flush=True)

        # 看门狗先武装：万一下面某一步卡住，也必须让进程真的消失
        self._watchdog = threading.Timer(WATCHDOG_SECONDS, self._force_exit)
        self._watchdog.daemon = True
        self._watchdog.start()

        steps = (
            ("停止语音", self._stop_speaking),
            ("停止远程服务", self._stop_remote),
            ("关闭控制台", self._close_console),
            ("关闭桌宠窗口", self._close_window),
            ("卸载托盘图标", self._hide_tray),
            ("关闭合成服务", self._shutdown_tts),
        )
        for label, fn in steps:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                # 任何一步失败都不能拦住退出 —— 这正是要解决的问题
                print(f"[Exit] {label}失败（继续退出）：{exc}", flush=True)

        # 让事件循环把上面那些 close() 的收尾事件处理完再退
        QTimer.singleShot(0, self.app.quit)

    def cancel_watchdog(self) -> None:
        """正常退出路径走完时调用，免得看门狗又打印一遍。"""
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

    def _force_exit(self) -> None:
        print(
            f"[Exit] {WATCHDOG_SECONDS:.0f} 秒内没能正常退出，强制结束进程"
            "（某个组件卡在清理里了）",
            flush=True,
        )
        # 这里**不能**再调 live2d.dispose()：卡住的往往正是它。
        # 显存/句柄由操作系统回收，比留一个后台进程干净得多。
        os._exit(0)

    # -------------------------------------------------------------- 各步实现
    def _stop_speaking(self) -> None:
        if self.window is not None:
            self.window.stop_speaking()

    def _stop_remote(self) -> None:
        if self.remote is not None:
            self.remote.stop()

    def _close_console(self) -> None:
        if self.console is not None:
            self.console.hide()

    def _close_window(self) -> None:
        """关桌宠窗口。

        这里**不能**再走 closeEvent → QApplication.quit() 那条路，
        因为 closeEvent 里还有一整套收尾（TTS、摄像头、Live2D），
        已经在上面按顺序做完了；重复做一遍只会让「谁先谁后」变得不确定。
        """
        if self.window is None:
            return
        try:
            self.window.prepare_shutdown()
        except Exception as exc:  # noqa: BLE001
            print(f"[Exit] 桌宠收尾出错（继续）：{exc}")
        self.window.hide()

    def _hide_tray(self) -> None:
        if self.tray is not None:
            self.tray.hide()

    def _shutdown_tts(self) -> None:
        """关掉 TTS 合成服务。

        GPT-SoVITS 是**独立进程**且占约 2.2GB 显存，不显式收掉的话，
        桌宠退出后它会变成孤儿进程一直占着显存 —— 这是本项目历史上真出现过的问题。
        """
        if self.tts is not None:
            self.tts.shutdown()
