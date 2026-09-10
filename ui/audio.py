"""音频播放：sounddevice 直接播 WAV，无需浏览器。

之所以统一产出 WAV，是因为口型同步用的 live2d.utils.lipsync.WavHandler
也只吃 WAV —— 播放和口型读同一份文件，天然同步。
"""
from __future__ import annotations

import wave
from pathlib import Path
from typing import Optional

import numpy as np
import sounddevice as sd


def wav_duration(path: Path) -> float:
    """读取 WAV 时长（秒）。"""
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate() or 1
            return handle.getnframes() / float(rate)
    except Exception:  # noqa: BLE001
        return 0.0


def wav_to_float32(path: Path) -> tuple[np.ndarray, int]:
    """读 WAV → (float32 波形, 采样率)，单声道返回一维、多声道返回 (n, ch)。"""
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())

    if width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 1:
        data = (np.frombuffer(raw, dtype="u1").astype(np.float32) - 128.0) / 128.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"unsupported sample width: {width}")

    if channels > 1:
        data = data.reshape(-1, channels)
    return data, rate


class AudioPlayer:
    """极简播放器：sounddevice 全局流，支持中断。"""

    def __init__(self) -> None:
        self._playing = False

    def play(self, path: Path) -> float:
        """播放 WAV，返回时长（秒）。非阻塞。"""
        self.stop()
        path = Path(path)
        if not path.exists():
            return 0.0
        try:
            data, rate = wav_to_float32(path)
        except Exception:  # noqa: BLE001
            return 0.0
        if data.size == 0:
            return 0.0
        try:
            sd.play(data, rate)
            self._playing = True
        except Exception:  # noqa: BLE001
            self._playing = False
            return 0.0
        return data.shape[0] / float(rate)

    def stop(self) -> None:
        try:
            sd.stop()
        except Exception:  # noqa: BLE001
            pass
        self._playing = False

    @property
    def playing(self) -> bool:
        return self._playing
