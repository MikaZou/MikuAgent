"""WebSocket 协议联调：模拟手机端走一遍完整对话。

用法::

    python tools/test_remote_ws.py              # 文字对话
    python tools/test_remote_ws.py --audio      # 顺带测语音上传转写
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "backend"))

import config  # noqa: E402

URL = f"ws://127.0.0.1:{config.REMOTE_PORT}/ws"


async def recv_until(ws, kinds: set[str], timeout: float = 120.0) -> dict:
    """一直读到出现 kinds 里的类型为止。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = await asyncio.wait_for(ws.recv(), timeout=max(1.0, deadline - time.time()))
        msg = json.loads(raw)
        print(f"    ← {msg.get('type')}: "
              f"{str(msg.get('text') or msg.get('message') or '')[:60]}"
              f"{'  [音频 %d KB]' % (len(msg.get('data',''))*3//4//1024) if msg.get('data') else ''}")
        if msg.get("type") in kinds:
            return msg
        if msg.get("type") == "error":
            return msg
    raise TimeoutError(f"等不到 {kinds}")


async def main() -> int:
    import websockets

    print("=" * 68)
    print(f"WebSocket 协议联调   {URL}")
    print("=" * 68)

    async with websockets.connect(URL, max_size=32 * 1024 * 1024) as ws:
        ready = await recv_until(ws, {"ready"}, timeout=15)
        print(f"    provider = {json.dumps(ready.get('provider'), ensure_ascii=False)}")

        # ---- 1) 文字对话 ----
        print("\n  [1] 文字对话")
        t0 = time.time()
        await ws.send(json.dumps({"type": "chat", "text": "你好呀 Miku"}))

        reply = await recv_until(ws, {"reply"}, timeout=120)
        t_reply = time.time() - t0
        print(f"      ⏱ 回复 {t_reply:.2f}s  情感={reply.get('emotion')}")
        print(f"      文本: {reply.get('text', '')[:70]}")

        speech = await recv_until(ws, {"speech"}, timeout=120)
        t_speech = time.time() - t0
        if speech.get("type") == "speech":
            size = len(speech.get("data", "")) * 3 // 4
            print(f"      ⏱ 语音就绪 {t_speech:.2f}s  {speech.get('duration'):.2f}s / {size/1024:.0f} KB")
            # 落盘核对是不是合法 WAV
            out = BASE / ".tmp" / "audio" / "ws_speech.wav"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(base64.b64decode(speech["data"]))
            import soundfile as sf
            d, sr = sf.read(str(out))
            print(f"      已存 {out.relative_to(BASE)}  解析: {len(d)/sr:.2f}s @ {sr}Hz")

        # ---- 2) 语音上传转写 ----
        if "--audio" in sys.argv:
            print("\n  [2] 语音上传（用 v4c 参考音频当录音）")
            src = BASE / "assets" / "voice" / "miku_v4c" / "miku_v4c_ref_5s.wav"
            import numpy as np
            import soundfile as sf

            data, sr = sf.read(str(src), always_2d=True)
            mono = data.mean(axis=1).astype("float32")
            # 手机端会转成 16k 单声道再上传
            if sr != 16000:
                import av
                rs = av.AudioResampler(format="fltp", layout="mono", rate=16000)
                fr = av.AudioFrame.from_ndarray(mono.reshape(1, -1), format="flt", layout="mono")
                fr.sample_rate = sr
                parts = [o.to_ndarray().reshape(-1) for o in rs.resample(fr)]
                parts += [o.to_ndarray().reshape(-1) for o in rs.resample(None)]
                mono = np.concatenate(parts)
                sr = 16000
            import io

            buf = io.BytesIO()
            sf.write(buf, mono, sr, format="WAV", subtype="PCM_16")
            b64 = base64.b64encode(buf.getvalue()).decode()

            t1 = time.time()
            await ws.send(json.dumps({"type": "audio", "data": b64}))
            tr = await recv_until(ws, {"transcript"}, timeout=120)
            print(f"      ⏱ 转写 {time.time()-t1:.2f}s  结果: {tr.get('text')!r}")
            # 转写后会自动接一轮对话
            await recv_until(ws, {"reply"}, timeout=120)
            await recv_until(ws, {"speech"}, timeout=120)

        # ---- 3) ping ----
        print("\n  [3] ping/pong")
        await ws.send(json.dumps({"type": "ping"}))
        await recv_until(ws, {"pong"}, timeout=10)

    print("\n  结论: WebSocket 协议链路可用 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
