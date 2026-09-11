"""UI 夹具：只起窗口与视觉按钮，不加载 TTS/STT（避开内存峰值）。

用于在本机内存吃紧时验证界面布局（尤其是新增的 📹 / 🖥️ 按钮）。

    python tools/ui_probe.py [秒数]
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "backend"))

import live2d.v3 as live2d  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
from agent import MikuAgent  # noqa: E402
from memory import MemoryStore  # noqa: E402
from ui.pet_window import PetWindow  # noqa: E402


def main() -> int:
    hold = int(sys.argv[1]) if len(sys.argv) > 1 else 40

    live2d.init()
    app = QApplication([])
    memory = MemoryStore(config.DB_PATH)
    agent = MikuAgent(memory)
    # stt / tts 传 None：这个夹具只关心界面，不加载 Whisper 与合成服务
    win = PetWindow(memory, agent, None, None)
    win.start()

    ib = win.input_bar
    print(f"[probe] 视频按钮可见={ib.video.isVisible()} 截屏按钮可见={ib.screen.isVisible()}")
    print(f"[probe] 摄像头可用={__import__('ui.capture', fromlist=['x']).camera_available()}")
    QTimer.singleShot(1500, lambda: print(
        f"[probe] 1.5s 后: video={ib.video.isVisible()} screen={ib.screen.isVisible()} "
        f"checked={ib.video.isChecked()}"
    ))
    # 顺手点一下截屏，验证「已配图」状态与占位符
    QTimer.singleShot(3000, win.capture_screen_for_reply)
    QTimer.singleShot(3600, lambda: print(
        f"[probe] 截屏后: 待发图={win._pending_image is not None} "
        f"来源={win._pending_source} 占位符={ib.input.placeholderText()!r}"
    ))
    # 再开关一次视频模式，看失败路径提示
    QTimer.singleShot(4500, lambda: win.set_video_enabled(True))
    QTimer.singleShot(7000, lambda: print(
        f"[probe] 开视频后: enabled={win._video_enabled} "
        f"worker={win._camera_worker is not None} 预览可见={win.preview.isVisible()}"
    ))

    QTimer.singleShot(hold * 1000, lambda: (win.close(), app.quit()))
    app.exec()
    live2d.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
