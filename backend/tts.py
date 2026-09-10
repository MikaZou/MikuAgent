"""语音输出（TTS）：把 Miku 的回复文本合成为 WAV。

设计要点：
  - 产出统一为 **WAV**，因为播放（sounddevice）和口型（WavHandler）都吃 WAV，
    同一份文件 → 天然同步。
  - 两个可插拔引擎：
      edge   —— 微软在线 TTS（31KB 依赖，开箱可用，默认）
      sovits —— GSV-TTS-Lite / GPT-SoVITS（本地，可克隆初音音色，需另装）
  - 懒加载：重型依赖（torch / gsv_tts）只在真正用到 sovits 时才 import，
    保证未安装时不影响启动。
  - 带 LRU 磁盘缓存：Miku 有大量重复问候语，命中直接复用。
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import threading
from pathlib import Path
from typing import Optional

import numpy as np

import config

# --------------------------------------------------------------------- 文本清洗
# 白名单：CJK 汉字、CJK 标点、全角字符、ASCII 可打印
_ALLOWED = re.compile(r"[^\u3000-\u303F\u4E00-\u9FFF\uFF00-\uFFEF\u0020-\u007E]")
_CJK = re.compile(r"[\u4E00-\u9FFF]")
_TAG_SQUARE = re.compile(r"\[[^\]]{0,24}\]")
_TAG_CJK = re.compile(r"【[^】]{0,24}】")


def normalize_text(text: str) -> str:
    """把带颜文字/emoji/装饰符号的回复清洗成可自然朗读的文本。

    人设会输出 ☆ ♪ (≧▽≦) （´▽｀）…… 这类内容，直接丢给 TTS 会读出怪音或杂音。
    保留句末的 。！？ —— 它们对语调有帮助；省略号/波浪号转成逗号做停顿。
    """
    if not text:
        return ""
    out = text

    # 1) 去掉残留的情感标签（正常已被 agent.parse_emotion 剥掉，这里兜底）
    out = _TAG_SQUARE.sub("", out)
    out = _TAG_CJK.sub("", out)

    # 2) 颜文字：括号内不含汉字就整体删掉，避免留下「（｀）」这种碎片
    def _drop_kaomoji(match: re.Match) -> str:
        inner = match.group(0)
        return inner if _CJK.search(inner) else ""

    out = re.sub(r"[\(\（][^\)\）]{0,24}[\)\）]", _drop_kaomoji, out)

    # 3) 省略号 / 波浪号 → 停顿（比直接删掉自然）
    out = re.sub(r"…+|。{2,}|\.{3,}", "，", out)
    out = re.sub(r"[~～]+", "，", out)

    # 4) 白名单过滤掉 emoji、假名、装饰符号
    out = _ALLOWED.sub("", out)

    # 5) 收尾规整
    out = re.sub(r"[，,]{2,}", "，", out)
    out = re.sub(r"^[\s，,、]+", "", out)
    out = re.sub(r"[\s，,、]+$", "", out)
    out = re.sub(r"\s{2,}", " ", out)
    # 6) 清洗后若只剩标点（例如整句都是日文假名被过滤掉），视为无内容
    if not re.search(r"[\u4E00-\u9FFFA-Za-z0-9]", out):
        return ""
    return out.strip()


# 情感 → (语速, 音调)。复用 agent 已经解析出的情感标签当语气。
EMOTION_PROSODY = {
    "HAPPY": ("+8%", "+35Hz"),
    "SAD": ("-8%", "+10Hz"),
    "ANGRY": ("+6%", "+12Hz"),
    "SURPRISED": ("+6%", "+45Hz"),
    "MOTIVATED": ("+12%", "+35Hz"),
    "EMPATHY": ("-5%", "+20Hz"),
    "NORMAL": ("+0%", "+25Hz"),
}


class TextToSpeech:
    """把文本合成为 WAV 文件。线程安全。"""

    def __init__(self) -> None:
        self.engine = config.TTS_ENGINE
        self.voice = config.TTS_VOICE
        self.base_rate = config.TTS_RATE
        self.base_pitch = config.TTS_PITCH
        self.volume = config.TTS_VOLUME
        self.device = config.TTS_DEVICE
        self.max_chars = config.TTS_MAX_CHARS
        self.cache_dir = Path(config.TTS_CACHE_DIR)

        self._lock = threading.Lock()
        self._status = "idle"
        self._detail = ""
        self._gsv = None

        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 状态
    @property
    def status(self) -> str:
        if self.engine in ("none", "off", ""):
            return "disabled"
        if self.engine == "edge":
            return self._edge_status()
        if self.engine == "sovits":
            return self._status
        return "unavailable"

    @property
    def status_detail(self) -> str:
        return self._detail

    def _edge_status(self) -> str:
        try:
            import edge_tts  # noqa: F401

            return "ready"
        except Exception:  # noqa: BLE001
            return "unavailable"

    # ------------------------------------------------------------------ 主入口
    def synthesize(self, text: str, emotion: str = "NORMAL") -> Optional[tuple[Path, float]]:
        """返回 (wav 路径, 时长秒)；不可用/无内容时返回 None。"""
        clean = normalize_text(text)
        if not clean:
            return None
        if len(clean) > self.max_chars:
            clean = clean[: self.max_chars]

        cache_key = self._cache_key(clean, emotion)
        cached = self.cache_dir / f"{cache_key}.wav"
        if cached.exists() and cached.stat().st_size > 1024:
            from ui.audio import wav_duration

            return cached, wav_duration(cached)

        with self._lock:
            if cached.exists() and cached.stat().st_size > 1024:
                from ui.audio import wav_duration

                return cached, wav_duration(cached)
            try:
                if self.engine == "edge":
                    path = self._synth_edge(clean, emotion, cached)
                elif self.engine == "sovits":
                    path = self._synth_sovits(clean, emotion, cached)
                else:
                    return None
            except Exception as exc:  # noqa: BLE001
                self._status = "error"
                self._detail = str(exc)
                print(f"[TTS] 合成失败（{self.engine}）：{exc}")
                return None

        if path is None or not Path(path).exists():
            return None
        from ui.audio import wav_duration

        return Path(path), wav_duration(Path(path))

    def _cache_key(self, text: str, emotion: str) -> str:
        rate, pitch = EMOTION_PROSODY.get((emotion or "NORMAL").upper(), (self.base_rate, self.base_pitch))
        raw = f"{self.engine}|{self.voice}|{rate}|{pitch}|{self.volume}|{text}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ edge
    def _synth_edge(self, text: str, emotion: str, out_wav: Path) -> Optional[Path]:
        import edge_tts

        rate, pitch = EMOTION_PROSODY.get(
            (emotion or "NORMAL").upper(), (self.base_rate, self.base_pitch)
        )
        tmp_mp3 = out_wav.with_suffix(".mp3.part")

        async def _run() -> None:
            communicate = edge_tts.Communicate(
                text,
                self.voice,
                rate=rate,
                pitch=pitch,
                volume=self.volume,
            )
            await communicate.save(str(tmp_mp3))

        asyncio.run(_run())

        if not tmp_mp3.exists() or tmp_mp3.stat().st_size == 0:
            raise RuntimeError("edge-tts 未返回音频")

        self._mp3_to_wav(tmp_mp3, out_wav)
        try:
            tmp_mp3.unlink()
        except OSError:
            pass
        self._status = "ready"
        return out_wav

    @staticmethod
    def _mp3_to_wav(src: Path, dst: Path, target_rate: int = 24000) -> None:
        """用 PyAV 把 mp3 解码重采样成 16bit 单声道 WAV（av 已是 faster-whisper 依赖）。"""
        import av
        import wave

        chunks: list[np.ndarray] = []
        with av.open(str(src)) as container:
            stream = container.streams.audio[0]
            resampler = av.AudioResampler(format="s16", layout="mono", rate=target_rate)
            for frame in container.decode(stream):
                for out in resampler.resample(frame):
                    chunks.append(out.to_ndarray().reshape(-1))
            for out in resampler.resample(None):
                chunks.append(out.to_ndarray().reshape(-1))

        if not chunks:
            raise RuntimeError("解码 mp3 失败")
        pcm = np.concatenate(chunks).astype(np.int16)
        tmp = dst.with_suffix(".wav.part")
        with wave.open(str(tmp), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(target_rate)
            handle.writeframes(pcm.tobytes())
        tmp.replace(dst)

    # ---------------------------------------------------------------- sovits
    def _ensure_gsv(self):
        """懒加载 GSV-TTS-Lite（重型，只有选 sovits 引擎才会走到）。"""
        if self._gsv is not None:
            return self._gsv
        self._status = "loading"
        try:
            from gsv_tts import TTS as GSVTTS  # type: ignore

            tts = GSVTTS()
            tts.load_gpt_model(config.TTS_GPT_MODEL or None)
            tts.load_sovits_model(config.TTS_SOVITS_MODEL or None)
            if config.TTS_REF_AUDIO:
                try:
                    tts.cache_spk_audio(config.TTS_REF_AUDIO)
                except Exception:  # noqa: BLE001
                    pass
            if config.TTS_PROMPT_AUDIO:
                try:
                    tts.cache_prompt_audio(
                        config.TTS_PROMPT_AUDIO, config.TTS_PROMPT_TEXT or ""
                    )
                except Exception:  # noqa: BLE001
                    pass
            self._gsv = tts
            self._status = "ready"
            return tts
        except Exception as exc:  # noqa: BLE001
            self._status = "error"
            self._detail = str(exc)
            raise

    def _synth_sovits(self, text: str, emotion: str, out_wav: Path) -> Optional[Path]:
        if not config.TTS_REF_AUDIO:
            raise RuntimeError("未配置 TTS_REF_AUDIO（初音音色参考音频）")
        tts = self._ensure_gsv()
        audio = tts.infer(
            spk_audio_path=config.TTS_REF_AUDIO,
            prompt_audio_path=config.TTS_PROMPT_AUDIO or None,
            prompt_audio_text=config.TTS_PROMPT_TEXT or None,
            text=text,
        )
        tmp = out_wav.with_suffix(".gsv.part")
        audio.save(str(tmp))
        Path(tmp).replace(out_wav)
        return out_wav
