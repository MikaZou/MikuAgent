"""画面采集：摄像头 / 截屏 / 剪贴板，统一输出 JPEG 字节。

三路采集共用同一条视觉管线，只是来源不同：

    摄像头 ─┐
    截屏   ─┼─▶ JPEG 字节 ─▶ backend.agent.chat(image=...)
    剪贴板 ─┘

摄像头依赖可选的 ``opencv-python-headless``。没装时 :func:`camera_available`
返回 False，界面上会隐藏摄像头按钮，但**截屏与剪贴板路径仍然可用**
（它们只用 PySide6 自带的 QScreen / QClipboard，零新增依赖）。

隐私约束：
  * 采集结果只存在于内存，本模块**不写任何文件**；
  * 缩放与 JPEG 编码都在本地完成，只有最终字节会随对话上传。
"""
from __future__ import annotations

import importlib.util
from typing import Optional

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage

import config


# --------------------------------------------------------------------- 编码
def _encode(image: QImage) -> Optional[bytes]:
    """等比缩放到 VISION_MAX_SIDE 以内，编码为 JPEG 字节。"""
    if image.isNull():
        return None
    limit = config.VISION_MAX_SIDE
    if limit > 0 and max(image.width(), image.height()) > limit:
        image = image.scaled(
            limit,
            limit,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buf, "JPEG", config.VISION_JPEG_QUALITY):
        return None
    return bytes(buf.data())


def camera_available() -> bool:
    """是否装了可选的摄像头依赖。

    **只探测、不导入**。这里刻意用 ``find_spec`` 而不是 ``import cv2``：
      * OpenCV 导入时会初始化 OpenCL，在共享 GPU 的机器上可能干扰
        另一个进程里的 CUDA 推理（实测：启动时 import cv2 会让 TTS 服务在
        ``cache_spk_audio`` 阶段抛 ``cudaErrorUnknown``）；
      * 不用摄像头的用户不该为它付出任何启动开销。
    真正需要 cv2 时（用户开启视频对话）才在 :class:`CameraSession` 里导入。
    """
    try:
        return importlib.util.find_spec("cv2") is not None
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ 屏幕/剪贴板
def capture_screen() -> Optional[bytes]:
    """截取主屏。截屏在多数系统上仍能拿到画面，即使桌面被其他窗口占据。"""
    app = QGuiApplication.instance()
    if app is None:
        return None
    screen = app.primaryScreen()
    if screen is None:
        return None
    try:
        pixmap = screen.grabWindow(0)
    except Exception:  # noqa: BLE001
        return None
    return _encode(pixmap.toImage())


def capture_clipboard() -> Optional[bytes]:
    """取剪贴板里的图片（复制一张图后按 Ctrl+V 的场景）。"""
    app = QGuiApplication.instance()
    if app is None:
        return None
    try:
        image = app.clipboard().image()
    except Exception:  # noqa: BLE001
        return None
    if image.isNull():
        return None
    return _encode(image)


# -------------------------------------------------------------------- 摄像头
class CameraSession:
    """摄像头会话。

    ``cv2.VideoCapture`` 不是线程安全的，约定只在**同一个线程**里使用。

    视频模式开启期间保持打开（而不是每次用的时候现开）：抓帧只要约 30ms，
    而 open 一次要 0.5~1 秒；同时摄像头 LED 常亮本身就是「正在工作」的
    可见指示，对用户是好事。
    """

    def __init__(self, index: int = -1) -> None:
        self.index = config.VISION_CAMERA_INDEX if index < 0 else index
        self._cap = None

    @property
    def is_open(self) -> bool:
        try:
            return self._cap is not None and self._cap.isOpened()
        except Exception:  # noqa: BLE001
            return False

    def open(self) -> tuple[bool, str]:
        """打开摄像头，返回 (成功, 失败原因)。

        注意**不能强制 DSHOW**：OpenCV 5.0 的 DSHOW 后端明确不支持按索引打开
        （``backend is generally available but can't be used to capture by index``），
        必须走默认后端（Windows 上是 MSMF）或显式 MSMF。
        这里按「默认 → MSMF」依次尝试，尽量兼容不同版本。
        """
        try:
            import cv2
        except Exception:  # noqa: BLE001
            return False, "未安装 opencv-python-headless，摄像头不可用"

        self.close()
        candidates: list[tuple[str, object]] = [("默认后端", None)]
        msmf = getattr(cv2, "CAP_MSMF", None)
        if msmf is not None:
            candidates.append(("MSMF", msmf))

        last_error = ""
        for label, backend in candidates:
            cap = None
            try:
                cap = (
                    cv2.VideoCapture(self.index)
                    if backend is None
                    else cv2.VideoCapture(self.index, backend)
                )
                if cap.isOpened():
                    # 部分后端 isOpened() 为真但读不到帧，这里试读一次再认账
                    ok, _ = cap.read()
                    if ok:
                        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                        self._cap = cap
                        return True, ""
                    last_error = f"{label} 能打开但读不到画面"
                else:
                    last_error = f"{label} 打不开设备"
            except Exception as exc:  # noqa: BLE001
                last_error = f"{label} 异常：{exc}"
            finally:
                if cap is not None and self._cap is None:
                    try:
                        cap.release()
                    except Exception:  # noqa: BLE001
                        pass

        return False, (
            f"打不开摄像头（索引 {self.index}）：{last_error or '未知原因'}。"
            "可能被其他程序占用，或系统未授予摄像头权限"
        )

    def read_frame(self) -> Optional[QImage]:
        """读一帧，返回 QImage（BGR 转 RGB）。"""
        if not self.is_open:
            return None
        try:
            ok, frame = self._cap.read()
        except Exception:  # noqa: BLE001
            return None
        if not ok or frame is None:
            return None
        try:
            h, w = frame.shape[:2]
            rgb = frame[:, :, ::-1].copy()          # BGR -> RGB
            img = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888)
            return img.copy()                        # 脱离 numpy 缓冲区
        except Exception:  # noqa: BLE001
            return None

    def read_jpeg(self) -> Optional[bytes]:
        image = self.read_frame()
        return _encode(image) if image is not None else None

    def close(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:  # noqa: BLE001
                pass
            self._cap = None


def probe_cameras(max_index: int = 4) -> list[int]:
    """探测哪些索引能打开摄像头（诊断用，会短暂占用设备）。

    与 :meth:`CameraSession.open` 用同一套后端策略，避免「诊断说没有、
    实际能用」这种不一致。
    """
    try:
        import cv2
    except Exception:  # noqa: BLE001
        return []
    found: list[int] = []
    msmf = getattr(cv2, "CAP_MSMF", None)
    for i in range(max_index):
        session = CameraSession(i)
        ok, _ = session.open()
        if ok:
            # 统一用同一套候选：这里只要 open() 说行就行
            found.append(i)
        session.close()
    return found
