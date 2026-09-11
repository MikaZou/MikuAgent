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

    def __init__(
        self,
        agent,
        session_id: Optional[int],
        message: str,
        image: Optional[bytes] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.agent = agent
        self.session_id = session_id
        self.message = message
        self.image = image            # JPEG 字节；None = 纯文本

    def run(self) -> None:  # noqa: D102
        try:
            result = self.agent.chat(self.session_id, self.message, self.image)
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


class TtsPipelineWorker(QThread):
    """逐句合成 + 依次播放。

    GPT-SoVITS 是「整段合成完才出声」，长回复要干等好几秒。
    按句切开：第一句合成完就交给主线程播放，同时后台接着合成第二句。
    这样首字延迟从「整段合成时间」降到「第一句合成时间」。
    """

    chunk_ready = Signal(str, float)  # (wav_path, duration_sec)
    all_done = Signal()
    failed = Signal(str)

    def __init__(self, tts, text: str, emotion: str, parent=None) -> None:
        super().__init__(parent)
        self.tts = tts
        self.text = text
        self.emotion = emotion
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:  # noqa: D102
        import time

        from tts import split_sentences  # 顶层导入避免循环依赖

        try:
            sentences = split_sentences(self.text)
        except Exception:  # noqa: BLE001
            sentences = [self.text]

        for sentence in sentences:
            if self._stop:
                return
            try:
                result = self.tts.synthesize(sentence, self.emotion)
            except Exception as exc:  # noqa: BLE001
                self.failed.emit(str(exc))
                continue
            if self._stop:
                return
            if result is None:
                continue
            path, duration = result
            self.chunk_ready.emit(str(path), float(duration))
            # 等这一段播完再合成下一段（实际播放发生在主线程）
            deadline = time.time() + duration + 0.08
            while time.time() < deadline:
                if self._stop:
                    return
                time.sleep(0.05)
        self.all_done.emit()
