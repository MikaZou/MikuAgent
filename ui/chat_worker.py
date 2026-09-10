"""后台工作线程：所有阻塞调用都必须离开 Qt 主线程。

Qt 要求 GUI 与 OpenGL 独占主线程，而下面这些调用都是秒级阻塞：
  - MikuAgent.chat()        → 网络 IO（DeepSeek）
  - SpeechToText.stop()     → Whisper 本地转写
  - TextToSpeech.synthesize() → 语音合成（首次还要加载模型）

统一走 QThread，结果通过信号回主线程更新 UI。
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QThread, Signal


class ChatWorker(QThread):
    """执行一轮对话。"""

    replied = Signal(dict)
    failed = Signal(str)

    def __init__(self, agent, session_id: Optional[int], message: str, parent=None) -> None:
        super().__init__(parent)
        self.agent = agent
        self.session_id = session_id
        self.message = message

    def run(self) -> None:  # noqa: D102
        try:
            result = self.agent.chat(self.session_id, self.message)
            self.replied.emit(result)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class TranscribeWorker(QThread):
    """停止录音并转写（阻塞）。"""

    transcribed = Signal(dict)

    def __init__(self, stt, parent=None) -> None:
        super().__init__(parent)
        self.stt = stt

    def run(self) -> None:  # noqa: D102
        try:
            self.transcribed.emit(self.stt.stop())
        except Exception as exc:  # noqa: BLE001
            self.transcribed.emit({"ok": False, "text": "", "duration": 0.0, "error": str(exc)})


class TtsWorker(QThread):
    """把回复文本合成为 WAV 文件（阻塞；首次可能加载模型）。"""

    synthesized = Signal(str, float)  # (wav_path, duration_sec) —— 空路径表示失败
    failed = Signal(str)

    def __init__(self, tts, text: str, emotion: str, parent=None) -> None:
        super().__init__(parent)
        self.tts = tts
        self.text = text
        self.emotion = emotion

    def run(self) -> None:  # noqa: D102
        try:
            result = self.tts.synthesize(self.text, self.emotion)
            if result is None:
                self.synthesized.emit("", 0.0)
            else:
                path, duration = result
                self.synthesized.emit(str(path), float(duration))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
