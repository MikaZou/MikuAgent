"""GPT-SoVITS 合成服务（独立进程）。

为什么要单独开一个进程：
    在 Qt 应用里、从后台线程首次 import torch 会稳定失败
    （torch.distributed._pycute/layout.py 的 `Self | int` 注解求值抛
     TypeError: Plain typing.Self is not valid as type argument，
     Python 3.10 + torch 2.11 的组合问题）；而在一个干净进程的主线程里导入完全正常。
    因此把重型导入和推理隔离到本进程，主程序通过本地 socket 调用。

协议：TCP + 一行一个 JSON。
    请求  {"text": "...", "emotion": "HAPPY", "out": "D:/.../x.wav"}
    响应  {"ok": true, "path": "...", "duration": 3.5}
          {"ok": false, "error": "..."}

就绪信号：模型加载完成后写 <port>.ready 文件；主程序据此判断可以发请求。

用法:
    python -u backend/tts_server.py [port]
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "backend"))

os.environ.setdefault("HF_ENDPOINT", os.getenv("STT_HF_ENDPOINT", "https://hf-mirror.com"))
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

import config  # noqa: E402

DEFAULT_PORT = 18520


def log(msg: str) -> None:
    print(f"[tts-server] {msg}", flush=True)


def build_engine():
    """在主线程里加载引擎（这一步在干净进程中是可靠的）。"""
    from gsv_tts import TTS as GSVTTS

    models_dir = Path(config.TTS_MODELS_DIR)
    models_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    engine = GSVTTS(
        models_dir=str(models_dir),
        device=config.TTS_DEVICE,
        use_bert=config.TTS_USE_BERT,
    )
    log(f"TTS() 构造完成 {time.time()-t0:.1f}s")
    engine.load_gpt_model() if not config.TTS_GPT_MODEL else engine.load_gpt_model(
        config.TTS_GPT_MODEL
    )
    engine.load_sovits_model() if not config.TTS_SOVITS_MODEL else engine.load_sovits_model(
        config.TTS_SOVITS_MODEL
    )
    log(f"模型加载完成 {time.time()-t0:.1f}s")

    ref = config.resolve_path(config.TTS_REF_AUDIO)
    if not ref.exists():
        raise RuntimeError(f"参考音频不存在：{ref}")
    engine.cache_spk_audio(str(ref))

    prompt = (
        config.resolve_path(config.TTS_PROMPT_AUDIO)
        if config.TTS_PROMPT_AUDIO
        else ref
    )
    return engine, ref, prompt


def handle(conn: socket.socket, engine, ref: Path, prompt: Path) -> None:
    try:
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
        req = json.loads(buf.decode("utf-8"))
        text = (req.get("text") or "").strip()
        out = Path(req["out"])
        if not text:
            conn.sendall(json.dumps({"ok": False, "error": "空文本"}).encode() + b"\n")
            return

        t0 = time.time()
        audio = engine.infer(
            spk_audio_path=str(ref),
            prompt_audio_path=str(prompt),
            prompt_audio_text=config.TTS_PROMPT_TEXT,
            text=text,
            text_language="zh",
            prompt_language="zh",
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.stem + ".srv.tmp.wav")
        audio.save(str(tmp))
        tmp.replace(out)
        gen = time.time() - t0

        import wave

        with wave.open(str(out), "rb") as f:
            dur = f.getnframes() / float(f.getframerate() or 1)
        log(f"合成完成 {dur:.2f}s 音频 / 耗时 {gen:.2f}s (RTF {gen/max(dur,1e-6):.2f})")
        conn.sendall(
            json.dumps({"ok": True, "path": str(out), "duration": dur}).encode() + b"\n"
        )
    except Exception as exc:  # noqa: BLE001
        log("合成失败：" + traceback.format_exc())
        try:
            conn.sendall(json.dumps({"ok": False, "error": str(exc)}).encode() + b"\n")
        except OSError:
            pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    ready = Path(config.DATA_DIR) / f"tts-server-{port}.ready"
    if ready.exists():
        ready.unlink()

    try:
        engine, ref, prompt = build_engine()
    except Exception:  # noqa: BLE001
        log("引擎加载失败：\n" + traceback.format_exc())
        return 1

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(4)
    ready.write_text("ready", encoding="utf-8")
    log(f"就绪，监听 127.0.0.1:{port}")

    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            break
        # 串行处理：GPU 推理本来也不能并发
        handle(conn, engine, ref, prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
