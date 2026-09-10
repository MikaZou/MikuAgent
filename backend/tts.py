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
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

# 必须在任何后台线程启动前导入 typing_extensions。
#
# 背景：stt.py 的 Whisper 加载线程与 tts.py 的模型预加载线程会并发首次导入
# typing_extensions（前者经 tokenizers/huggingface_hub，后者经 torch）。
# 在 Python 3.10 下这会让 typing_extensions.Self 处于未完成状态，
# 于是 torch.distributed._pycute/layout.py 里的 `Self | int` 注解求值直接抛
#   TypeError: Plain typing.Self is not valid as type argument
# 现象是「脚本里 import torch 没事，一进 App 就必失败」。
# 在这里顶层导入一次，即可保证两个线程启动前它已经完整加载。
import typing_extensions  # noqa: F401  (见上方说明，勿删)

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
        self._server = None          # sovits 合成服务子进程
        self._server_log = None

        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass

        # sovits 首次合成要加载约 2.4GB 模型（十几秒），放后台线程预热，
        # 否则「第一条回复」会让人以为卡死了。
        if self.engine == "sovits" and config.TTS_ENABLED:
            threading.Thread(target=self._preload, daemon=True).start()

    def _preload(self) -> None:
        """后台把合成服务拉起来（首次要加载约 2.4GB 模型，约 20s）。"""
        try:
            self._ensure_server(wait=True)
            print("[TTS] GPT-SoVITS 服务已就绪（初音音色）")
        except Exception as exc:  # noqa: BLE001
            print(f"[TTS] 服务启动失败：{exc}")

    # ------------------------------------------------------- sovits 服务进程
    def _port_open(self) -> bool:
        import socket as _socket

        try:
            with _socket.create_connection(
                ("127.0.0.1", config.TTS_SERVER_PORT), timeout=0.5
            ):
                return True
        except OSError:
            return False

    def _spawn_server(self) -> None:
        """启动独立的合成服务进程。

        为什么不用线程内导入：在 Qt 应用的后台线程里首次 import torch 会稳定失败
        （Python 3.10 + torch 2.11 的 typing.Self 注解问题），而干净进程的主线程里正常。
        """
        import subprocess

        script = Path(__file__).resolve().parent / "tts_server.py"
        log_path = Path(config.DATA_DIR) / "tts-server.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "ab")

        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW

        # 4GB 显存还要和桌宠共享，降低 PyTorch 的显存碎片
        env = os.environ.copy()
        env.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

        self._server_log = log_file
        self._server = subprocess.Popen(
            [sys.executable, "-u", str(script), str(config.TTS_SERVER_PORT)],
            cwd=str(config.BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            creationflags=creationflags,
            env=env,
        )
        print(f"[TTS] 已启动合成服务进程 pid={self._server.pid}（日志 data/tts-server.log）")

    def _ensure_server(self, wait: bool = True) -> bool:
        if self._port_open():
            return True
        if self._server is None or self._server.poll() is not None:
            self._spawn_server()
        if not wait:
            return False

        deadline = time.time() + config.TTS_SERVER_TIMEOUT
        while time.time() < deadline:
            if self._port_open():
                return True
            if self._server.poll() is not None:
                raise RuntimeError(
                    f"合成服务进程退出（code={self._server.returncode}），"
                    "详见 data/tts-server.log"
                )
            time.sleep(0.5)
        raise TimeoutError(f"合成服务 {config.TTS_SERVER_TIMEOUT}s 内未就绪")

    def shutdown(self) -> None:
        """退出时收掉服务进程。"""
        proc = getattr(self, "_server", None)
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        self._server = None

    # ------------------------------------------------------------------ 状态
    @property
    def status(self) -> str:
        if self.engine in ("none", "off", ""):
            return "disabled"
        if self.engine == "edge":
            return self._edge_status()
        if self.engine == "sovits":
            if self._status in ("loading", "ready", "error"):
                return self._status
            return self._sovits_available()
        return "unavailable"

    @staticmethod
    def _sovits_available() -> str:
        """只检查依赖是否装了，不真的 import（import gsv_tts 要十几秒，会卡住 UI）。"""
        import importlib.util

        for mod in ("gsv_tts", "torch", "torchaudio"):
            try:
                if importlib.util.find_spec(mod) is None:
                    return "unavailable"
            except (ImportError, ValueError):
                return "unavailable"
        return "ready"

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
    def _synth_sovits(self, text: str, emotion: str, out_wav: Path) -> Optional[Path]:
        """通过独立进程合成（初音音色）。

        不走进程内导入的原因见 _spawn_server 的说明。
        """
        import json
        import socket

        if not config.TTS_REF_AUDIO:
            raise RuntimeError("未配置 TTS_REF_AUDIO（初音音色参考音频）")
        if not config.TTS_PROMPT_TEXT:
            raise RuntimeError(
                "未配置 TTS_PROMPT_TEXT（参考音频的转写文本，GPT-SoVITS 必填）"
            )

        self._ensure_server(wait=True)

        payload = json.dumps(
            {"text": text, "emotion": emotion, "out": str(out_wav)}, ensure_ascii=False
        ).encode("utf-8") + b"\n"

        with socket.create_connection(
            ("127.0.0.1", config.TTS_SERVER_PORT), timeout=config.TTS_SYNTH_TIMEOUT
        ) as sock:
            sock.settimeout(config.TTS_SYNTH_TIMEOUT)
            sock.sendall(payload)

            buf = b""
            while not buf.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk

        if not buf.strip():
            raise RuntimeError("合成服务没有返回结果")
        resp = json.loads(buf.decode("utf-8"))
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error") or "合成失败")

        path = Path(resp["path"])
        if not path.exists():
            raise RuntimeError(f"合成服务声称成功但文件不存在：{path}")
        self._status = "ready"
        return path

