"""验证 MiniMax API 可用性：TTS 合成 + 音色列表 + ASR 连通性。

用法::

    python tools/test_minimax.py            # 只测 TTS
    python tools/test_minimax.py --play     # 合成后播放
    python tools/test_minimax.py --asr      # 顺带测语音识别
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "backend"))

import config  # noqa: E402

OUT = BASE / ".tmp" / "audio"
TEST_TEXT = "主人早上好呀，今天想和 Miku 一起做什么呢？"
# 先用官方系统音色验证通路，音色克隆成功后再换成克隆 ID
FALLBACK_VOICE = "female-shaonv"


def redact(s: str) -> str:
    """任何输出都不要泄露完整 key。"""
    if not s:
        return s
    return s[:8] + "…" + s[-4:] if len(s) > 16 else "***"


def post_json(url: str, payload: dict, timeout: int = 120) -> tuple[int, dict]:
    import requests

    headers = {
        "Authorization": f"Bearer {config.MINIMAX_API_KEY}",
        "Content-Type": "application/json",
    }
    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    try:
        return r.status_code, r.json()
    except Exception:  # noqa: BLE001
        return r.status_code, {"_raw": r.text[:400]}


def test_tts(play: bool) -> bool:
    print("=" * 68)
    print("MiniMax TTS 连通性")
    print("=" * 68)
    print(f"  base_url : {config.MINIMAX_BASE_URL}")
    print(f"  key      : {redact(config.MINIMAX_API_KEY)}")
    print(f"  model    : {config.MINIMAX_TTS_MODEL}")

    voice = config.MINIMAX_VOICE_ID or FALLBACK_VOICE
    print(f"  voice_id : {voice}"
          f"{'  (系统音色，克隆后再换)' if not config.MINIMAX_VOICE_ID else '  (已克隆)'}")

    payload = {
        "model": config.MINIMAX_TTS_MODEL,
        "text": TEST_TEXT,
        "stream": False,
        "voice_setting": {"voice_id": voice, "speed": 1, "vol": 1, "pitch": 0},
        "audio_setting": {
            "sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 1,
        },
    }
    t0 = time.time()
    try:
        code, data = post_json(f"{config.MINIMAX_BASE_URL}/v1/t2a_v2", payload)
    except Exception as exc:  # noqa: BLE001
        print(f"  请求异常：{type(exc).__name__}: {exc}")
        return False
    dt = time.time() - t0

    print(f"  HTTP {code}  耗时 {dt:.2f}s")
    if code != 200:
        print(f"  响应：{json.dumps(data, ensure_ascii=False)[:400]}")
        return False

    base = data.get("base_resp", {})
    if base.get("status_code") not in (0, None):
        print(f"  业务错误：{base}")
        return False

    audio_hex = (data.get("data") or {}).get("audio") or ""
    info = data.get("extra_info") or {}
    if not audio_hex:
        print(f"  没有返回音频：{json.dumps(data, ensure_ascii=False)[:300]}")
        return False

    audio = bytes.fromhex(audio_hex)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "minimax_test.mp3"
    path.write_bytes(audio)

    print(f"  OK 音频 {len(audio)/1024:.1f} KB")
    print(f"     时长 {info.get('audio_length', '?')} ms  "
          f"采样率 {info.get('audio_sample_rate', '?')}  "
          f"字数 {info.get('usage_characters', '?')}")
    print(f"  已保存：{path.relative_to(BASE)}")

    # mp3 -> wav 便于试听与后续处理
    try:
        import av
        import numpy as np
        import soundfile as sf

        container = av.open(str(path))
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=44100)
        chunks = []
        for frame in container.decode(container.streams.audio[0]):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        chunks += [o.to_ndarray().reshape(-1) for o in resampler.resample(None)]
        container.close()
        wav = OUT / "minimax_test.wav"
        sf.write(str(wav), np.concatenate(chunks), 44100, subtype="PCM_16")
        print(f"  已转 WAV：{wav.relative_to(BASE)}")
    except Exception as exc:  # noqa: BLE001
        print(f"  （mp3 转 WAV 失败，不影响连通性判断：{exc}）")

    if play:
        try:
            import sounddevice as sd
            import soundfile as sf

            d, sr = sf.read(str(OUT / "minimax_test.wav"), always_2d=True)
            print("  ▶ 播放合成结果 …")
            sd.play(d, sr)
            time.sleep(len(d) / sr + 0.4)
            sd.stop()
        except Exception as exc:  # noqa: BLE001
            print(f"  播放失败：{exc}")
    return True


def test_asr() -> bool:
    print()
    print("=" * 68)
    print("MiniMax ASR 连通性")
    print("=" * 68)
    import requests

    src = BASE / "assets" / "voice" / "miku_v4c" / "miku_v4c_ref_5s.wav"
    if not src.exists():
        print(f"  找不到测试音频 {src}")
        return False

    url = f"{config.MINIMAX_BASE_URL}/v1/speech_to_text"
    headers = {"Authorization": f"Bearer {config.MINIMAX_API_KEY}"}
    files = {"file": (src.name, src.read_bytes(), "audio/wav")}
    data = {"model": config.MINIMAX_ASR_MODEL, "response_format": "json"}
    t0 = time.time()
    try:
        r = requests.post(url, headers=headers, data=data, files=files, timeout=180)
    except Exception as exc:  # noqa: BLE001
        print(f"  请求异常：{type(exc).__name__}: {exc}")
        return False
    dt = time.time() - t0
    print(f"  HTTP {r.status_code}  耗时 {dt:.2f}s")
    try:
        j = r.json()
    except Exception:  # noqa: BLE001
        print(f"  响应：{r.text[:300]}")
        return False
    if r.status_code != 200:
        print(f"  响应：{json.dumps(j, ensure_ascii=False)[:400]}")
        return False
    print(f"  识别结果：{j.get('text', '')!r}")
    print(f"  时长：{j.get('duration', '?')}s")
    expect = "大家好，我是初音未来"
    ok = expect[:4] in (j.get("text") or "")
    print(f"  与预期「{expect}」比对：{'匹配 ✅' if ok else '不完全匹配（可接受）'}")
    return True


def main() -> int:
    if not config.MINIMAX_API_KEY or config.MINIMAX_API_KEY.startswith("xxx"):
        print("  未配置 MINIMAX_API_KEY")
        return 1
    ok = test_tts("--play" in sys.argv)
    if "--asr" in sys.argv:
        ok = test_asr() and ok
    print()
    print("  结论：" + ("MiniMax API 可用 ✅" if ok else "MiniMax API 不可用 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
