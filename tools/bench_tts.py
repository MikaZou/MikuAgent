"""量化 TTS 延迟与情感调节效果。

1) 不同长度文本的「合成耗时 / 音频时长 / RTF」—— 用于观察 CUDA graph 预热效果
2) 不同 emotion 说同一句话 —— 验证情感是否真的改变了语速

用法:
    python tools/bench_tts.py            # 需要合成服务已在运行
"""
from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "backend"))

import config  # noqa: E402

CASES = [
    ("短", "好的，主人！"),
    ("中", "主人今天心情不错呢，要不要一起听首歌呀？"),
    ("长", "诶嘿，主人问我今天做了什么呀？Miku 今天练了好久的新歌，还偷偷吃了一颗草莓，"
           "超级甜的哦，主人要不要也尝一口呢？"),
]

EMO_SENTENCE = "主人今天过得怎么样呀，要不要一起听首歌呢？"
EMOTIONS = ["MOTIVATED", "HAPPY", "NORMAL", "EMPATHY", "SAD"]


def synth(text: str, out: Path, emotion: str = "NORMAL",
          speed: float | None = None) -> tuple[float, float]:
    """返回 (合成耗时, 音频时长)。"""
    body: dict = {"text": text, "emotion": emotion, "out": str(out)}
    if speed is not None:
        body["speed"] = speed
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8") + b"\n"
    t0 = time.time()
    with socket.create_connection(
        ("127.0.0.1", config.TTS_SERVER_PORT), timeout=300
    ) as sock:
        sock.settimeout(300)
        sock.sendall(payload)
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    elapsed = time.time() - t0
    resp = json.loads(buf.decode("utf-8"))
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error"))
    return elapsed, float(resp.get("duration") or 0.0)


def wait_ready(timeout: float = 400.0) -> float:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with socket.create_connection(
                ("127.0.0.1", config.TTS_SERVER_PORT), timeout=0.5
            ):
                return time.time() - t0
        except OSError:
            time.sleep(0.5)
    raise TimeoutError("合成服务未就绪")


def main() -> int:
    print(f"等待合成服务（端口 {config.TTS_SERVER_PORT}，含预热可能要几十秒）...")
    waited = wait_ready()
    print(f"服务就绪，等待 {waited:.0f}s\n")

    outdir = BASE / "data" / "tts-cache"
    outdir.mkdir(parents=True, exist_ok=True)

    print("=== 不同长度：合成耗时 ===")
    print(f"{'用例':<6} {'字数':>4} {'音频':>7} {'合成':>7} {'RTF':>6}")
    print("-" * 40)
    for name, text in CASES:
        out = outdir / f"bench_{len(text)}.wav"
        out.unlink(missing_ok=True)
        elapsed, dur = synth(text, out)
        print(f"{name:<6} {len(text):>4} {dur:>6.2f}s {elapsed:>6.2f}s {elapsed/max(dur,1e-6):>6.2f}")

    print("\n=== 情感 → 语速（同一句话）===")
    print(f"{'情感':<10} {'音频时长':>8} {'相对NORMAL':>10} {'合成':>7}")
    print("-" * 42)
    base_dur = None
    for emo in EMOTIONS:
        out = outdir / f"bench_emo_{emo}.wav"
        out.unlink(missing_ok=True)
        elapsed, dur = synth(EMO_SENTENCE, out, emotion=emo)
        if emo == "NORMAL":
            base_dur = dur
        rel = f"{base_dur/dur:.3f}" if base_dur else "-"
        print(f"{emo:<10} {dur:>7.2f}s {rel:>10} {elapsed:>6.2f}s")
    print("\n（相对NORMAL >1 表示更短更快，<1 表示更长更慢）")

    # 决定性校验：直接指定 speed，看时长是否随之成比例变化。
    # 情感那组因为采样有随机性，顺序不一定严格单调；这组能确认参数真的到达了模型。
    print("\n=== speed 参数校验（同一句话，直接指定 speed）===")
    print(f"{'speed':>6} {'音频时长':>8} {'理论值/实测':>12}")
    print("-" * 32)
    ref = None
    for sp in (0.8, 1.0, 1.5):
        out = outdir / f"bench_speed_{sp}.wav"
        out.unlink(missing_ok=True)
        _, dur = synth(EMO_SENTENCE, out, speed=sp)
        if sp == 1.0:
            ref = dur
        ratio = f"{ref/dur:.3f}" if ref else "-"
        print(f"{sp:>6} {dur:>7.2f}s {ratio:>12}")
    print("（若 speed 生效，应约为 1/0.8=1.25 与 1/1.5=0.67）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
