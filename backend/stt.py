"""语音输入：麦克风录音（sounddevice）+ 本地转写（faster-whisper）。

按住说话流程：
    POST /api/mic/start   → 开始录音
    POST /api/mic/stop    → 停止录音并转写，返回文本
    POST /api/mic/cancel  → 放弃本次录音

Whisper 模型首次使用时会自动下载（默认 small，约 460MB），之后完全离线。
"""

import os
import threading
import time

import numpy as np
import sounddevice as sd

import config

# 必须在导入 faster_whisper 之前设置 HF 镜像，否则 huggingface_hub 可能已缓存官方地址
if config.STT_HF_ENDPOINT:
    os.environ.setdefault("HF_ENDPOINT", config.STT_HF_ENDPOINT)
# hf-mirror 不支持 Xet 存储，强制走普通 HTTP 下载；同时关闭 symlink 警告
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from faster_whisper import WhisperModel


class SpeechToText:
    """麦克风录音 + Whisper 本地转写。"""

    def __init__(self):
        self.model_size = config.STT_MODEL
        self.language = config.STT_LANGUAGE or None
        self.sample_rate = config.STT_SAMPLE_RATE
        self.min_duration = config.STT_MIN_DURATION

        self._model = None
        self._model_lock = threading.Lock()
        self._status = "loading"  # loading / ready / error
        self._status_detail = ""

        self._stream = None
        self._frames = []
        self._recording = False
        self._record_lock = threading.Lock()
        self._record_started_at = 0.0

        # 后台预加载模型，避免启动阻塞（首次需联网下载）
        threading.Thread(target=self._ensure_model, daemon=True).start()

    # ---------- 模型 ----------

    def _ensure_model(self):
        """线程安全地确保模型已加载，返回模型或 None。"""
        with self._model_lock:
            if self._model is not None:
                return self._model
            self._status = "loading"
            try:
                self._model = WhisperModel(
                    self.model_size,
                    device="cpu",
                    compute_type="int8",
                )
                self._status = "ready"
            except Exception as exc:  # noqa: BLE001
                self._status = "error"
                self._status_detail = str(exc)
                print(f"[STT] Whisper 模型加载失败：{exc}")
            return self._model

    @property
    def status(self):
        return self._status

    @property
    def status_detail(self):
        return self._status_detail

    # ---------- 录音 ----------

    def start(self):
        """开始录音。返回 (ok, message)。"""
        with self._record_lock:
            if self._recording:
                return False, "已经在录音了"
            try:
                self._frames = []
                self._stream = sd.InputStream(
                    samplerate=self.sample_rate,
                    channels=1,
                    dtype="float32",
                    callback=self._on_audio,
                )
                self._stream.start()
                self._recording = True
                self._record_started_at = time.time()
                return True, ""
            except Exception as exc:  # noqa: BLE001
                self._stream = None
                return False, f"打开麦克风失败：{exc}"

    def _on_audio(self, indata, frames, time_info, status):
        if status:
            print(f"[STT] 录音状态：{status}")
        self._frames.append(indata.copy())

    def stop(self):
        """停止录音并转写。返回 dict。"""
        with self._record_lock:
            if not self._recording:
                return {"ok": False, "text": "", "duration": 0.0, "error": "没有正在进行的录音"}
            self._close_stream_locked()
            frames = self._frames
            duration = time.time() - self._record_started_at
            self._recording = False
            self._frames = []

        if duration < self.min_duration or not frames:
            return {"ok": True, "text": "", "duration": duration, "error": ""}

        audio = np.concatenate(frames, axis=0).flatten()
        text = self._transcribe(audio)
        return {"ok": True, "text": text, "duration": duration, "error": ""}

    def cancel(self):
        """放弃本次录音，丢弃已录到的音频。"""
        with self._record_lock:
            if not self._recording:
                return
            self._close_stream_locked()
            self._frames = []
            self._recording = False

    def _close_stream_locked(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._stream = None

    # ---------- 转写 ----------

    def _transcribe(self, audio):
        model = self._ensure_model()
        if model is None:
            return ""
        try:
            segments, _info = model.transcribe(
                audio,
                language=self.language,
                vad_filter=True,
                beam_size=1,
            )
            return "".join(seg.text for seg in segments).strip()
        except Exception as exc:  # noqa: BLE001
            print(f"[STT] 转写失败：{exc}")
            return ""
