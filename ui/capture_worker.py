"""摄像头采集线程。

``cv2.VideoCapture`` 不是线程安全的，所以由 :class:`CameraWorker` **独占**设备：
它一边产出预览帧，一边把最新的 JPEG 缓存在内存里。

需要给对话配图时（说完话那一刻、或点了发送），直接取缓存的最新一帧，
而不是去抢设备现场抓 —— 缓存最多落后 ``1/VISION_PREVIEW_FPS`` 秒（默认 200ms），
人眼无法察觉，但换来了**零采集延迟**。

截屏与剪贴板**不在这里**：Qt 的 ``QScreen.grabWindow`` / ``QClipboard`` 有
GUI 线程亲和性，且耗时只有几十毫秒，在 ``ui/pet_window.py`` 里直接调用更安全。
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QMutex, QThread, Signal
from PySide6.QtGui import QImage

import config
from ui.capture import CameraSession, _encode


class CameraWorker(QThread):
    """独占摄像头的后台线程：产出预览帧 + 缓存最新一帧。"""

    frame_ready = Signal(QImage)
    failed = Signal(str)

    def __init__(self, session: CameraSession, parent=None) -> None:
        super().__init__(parent)
        self.session = session
        self._stop = False
        self._lock = QMutex()
        self._latest: Optional[bytes] = None
        self._consecutive_failures = 0

    # ------------------------------------------------------------------ 线程体
    def run(self) -> None:  # noqa: D102
        interval = max(1, int(1000 / max(1, config.VISION_PREVIEW_FPS)))
        while not self._stop:
            image = self.session.read_frame()
            if image is None:
                self._consecutive_failures += 1
                # 连续读不到（拔掉 / 被其他程序抢走）就报错退出，别一直空转
                if self._consecutive_failures >= 15:
                    self.failed.emit("摄像头读不到画面了，可能被其他程序占用了")
                    return
                self.msleep(interval)
                continue
            self._consecutive_failures = 0

            jpeg = _encode(image)
            if jpeg:
                self._lock.lock()
                self._latest = jpeg
                self._lock.unlock()
            if not self._stop:
                self.frame_ready.emit(image)
            self.msleep(interval)

    # ------------------------------------------------------------------ 对外
    def take_latest(self) -> Optional[bytes]:
        """取最近一帧的 JPEG。没有则返回 None（调用方应降级为纯文本）。"""
        self._lock.lock()
        data = self._latest
        self._lock.unlock()
        return data

    def stop(self) -> None:
        self._stop = True
