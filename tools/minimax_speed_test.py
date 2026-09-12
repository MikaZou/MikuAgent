"""测 MiniMax 语速参数对时长的影响，找出接近自然人声的取值。

背景：克隆音色默认 speed=1 时，21 个汉字合成出约 9.9 秒音频（2.1 字/秒），
而本地 GPT-SoVITS 同样文本只有 4.6 秒。中文正常语速约 4~5 字/秒。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "backend"))

import config  # noqa: E402

TEXT = "主人早上好呀，今天想和 Miku 一起做什么呢？"
SPEEDS = [1.0, 1.2, 1.5, 1.8, 2.0]


def synth(speed: float, save_to: Path | None = None) -> tuple[float, float, int]:
    import requests

    payload = {
        "model": config.MINIMAX_TTS_MODEL,
        "text": TEXT,
        "stream": False,
        "voice_setting": {
            "voice_id": config.MINIMAX_VOICE_ID,
            "speed": speed, "vol": 1, "pitch": 0,
        },
        "audio_setting": {"sample_rate": 32000, "format": "mp3", "channel": 1},
    }
    t0 = time.time()
    r = requests.post(
        f"{config.MINIMAX_BASE_URL}/v1/t2a_v2",
        headers={"Authorization": f"Bearer {config.MINIMAX_API_KEY}",
                 "Content-Type": "application/json"},
        json=payload, timeout=180,
    )
    dt = time.time() - t0
    j = r.json()
    if (j.get("base_resp") or {}).get("status_code") not in (0, None):
        raise RuntimeError(json.dumps(j, ensure_ascii=False)[:200])
    info = j.get("extra_info") or {}
    audio = bytes.fromhex((j.get("data") or {}).get("audio") or "")
    if save_to is not None and audio:
        save_to.parent.mkdir(parents=True, exist_ok=True)
        save_to.write_bytes(audio)
    return dt, info.get("audio_length", 0) / 1000.0, len(audio)


def play_mp3(path: Path) -> None:
    import av
    import numpy as np
    import sounddevice as sd

    c = av.open(str(path))
    rs = av.AudioResampler(format="fltp", layout="mono", rate=44100)
    chunks = []
    for fr in c.decode(c.streams.audio[0]):
        chunks += [o.to_ndarray().reshape(-1) for o in rs.resample(fr)]
    chunks += [o.to_ndarray().reshape(-1) for o in rs.resample(None)]
    c.close()
    d = np.concatenate(chunks)
    print(f"    ▶ 播放 speed={path.stem.split('_')[-1]}  ({len(d)/44100:.2f}s)")
    sd.play(d, 44100)
    time.sleep(len(d) / 44100 + 0.5)
    sd.stop()


def main() -> int:
    n = len([c for c in TEXT if not c.isspace()])
    print("=" * 70)
    print(f"语速参数测试   文本 {n} 字   音色 {config.MINIMAX_VOICE_ID}")
    print("=" * 70)
    print(f"  {'speed':>6} {'音频时长':>9} {'字/秒':>7} {'合成耗时':>9} {'大小':>9}  评估")
    print("  " + "-" * 62)
    for sp in SPEEDS:
        try:
            dt, dur, size = synth(sp)
        except Exception as exc:  # noqa: BLE001
            print(f"  {sp:>6} 失败：{exc}")
            continue
        cps = n / dur if dur else 0
        if cps < 2.8:
            judge = "偏慢"
        elif cps <= 5.2:
            judge = "接近自然 ✅"
        else:
            judge = "偏快"
        print(f"  {sp:>6.2f} {dur:>8.2f}s {cps:>7.2f} {dt:>8.2f}s {size/1024:>8.1f}KB  {judge}")
    print()
    print("  参考：中文自然语速约 4~5 字/秒；本地 GPT-SoVITS 同文本约 4.6 秒(4.6 字/秒)")

    if "--play" in sys.argv:
        out = BASE / ".tmp" / "audio"
        print()
        print("  对比试听（同文本、不同语速）：")
        for sp in (1.0, 2.0):
            p = out / f"minimax_speed_{sp}.mp3"
            try:
                synth(sp, save_to=p)
                play_mp3(p)
            except Exception as exc:  # noqa: BLE001
                print(f"    speed={sp} 失败：{exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
