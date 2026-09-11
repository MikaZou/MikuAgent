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

# 情感 → 语速。GSV 的 infer() 只暴露 speed 这一个韵律参数，
# 用它让不同情绪听起来有区别（NORMAL=1.0 为基准）。
EMOTION_SPEED = {
    "HAPPY": 1.08,
    "MOTIVATED": 1.12,
    "SURPRISED": 1.06,
    "ANGRY": 1.06,
    "EMPATHY": 0.95,
    "SAD": 0.92,
    "NORMAL": 1.0,
}

# 启动预热用的文本。长度覆盖短/中/长，让 CUDA graph 提前捕获好，
# 否则「每条新长度的回复」第一次都要现捕获，会慢 5~8 倍。
WARMUP_TEXTS = [
    "你好呀。",
    "主人今天过得怎么样呀？",
    "诶嘿，主人来啦！Miku 今天练了好久的新歌，还偷偷吃了一颗草莓，"
    "超级甜的哦，主人要不要也尝一口呢？",
]


def log(msg: str) -> None:
    print(f"[tts-server] {msg}", flush=True)


def build_engine():
    """在主线程里加载引擎（这一步在干净进程中是可靠的）。"""
    from gsv_tts import TTS as GSVTTS

    if config.TTS_DEVICE == "cpu":
        log("提示：TTS_DEVICE=cpu 在本机可能不可用 ——")
        log("  gsv_tts 的 choose_attention_backend() 是按 torch.cuda.is_available() 决策的，")
        log("  有显卡的机器即使强制 device=cpu 也会选到 CUDA 专用的 CUDNN_ATTENTION，")
        log("  随后在 CPU 张量上抛 'No viable backend for scaled_dot_product_attention'。")
        log("  无显卡建议改用 TTS_ENGINE=edge（在线、无需 GPU）。")

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

        # 情感 → 语速（GSV 的 infer 支持 speed；不复用这个参数的话，
        # 伤心和开心听起来会一模一样）
        emotion = (req.get("emotion") or "NORMAL").upper()
        speed = float(req.get("speed") or EMOTION_SPEED.get(emotion, 1.0))

        t0 = time.time()
        audio = engine.infer(
            spk_audio_path=str(ref),
            prompt_audio_path=str(prompt),
            prompt_audio_text=config.TTS_PROMPT_TEXT,
            text=text,
            text_language="zh",
            prompt_language="zh",
            speed=speed,
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


def warmup(engine, ref: Path, prompt: Path) -> None:
    """预热 CUDA graph。

    GSV 用静态 CUDA graph 缓存（gpt_cache），每种序列长度第一次推理都要
    现捕获计算图，实测同一条文本第 1 次 9.6s、第 3 次降到 1.9s（RTF 0.72 → 0.20）。
    启动时先按几种典型长度各跑两遍，用户后面就一直是稳态速度。
    """
    if not config.TTS_WARMUP:
        log("已跳过预热（TTS_WARMUP=false）")
        return

    t0 = time.time()
    total = len(WARMUP_TEXTS) * 2  # 每种长度跑两遍，第一遍捕获、第二遍确认
    n = 0
    for text in WARMUP_TEXTS:
        for _ in range(2):
            n += 1
            try:
                engine.infer(
                    spk_audio_path=str(ref),
                    prompt_audio_path=str(prompt),
                    prompt_audio_text=config.TTS_PROMPT_TEXT,
                    text=text,
                    text_language="zh",
                    prompt_language="zh",
                )
                log(f"预热 {n}/{total} 完成（累计 {time.time()-t0:.1f}s）")
            except Exception as exc:  # noqa: BLE001
                log(f"预热 {n}/{total} 失败（不影响使用）：{exc}")
    log(f"预热结束，共 {time.time()-t0:.1f}s")


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

    warmup(engine, ref, prompt)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", port))
    except OSError as exc:
        log(f"端口 {port} 绑定失败：{exc}")
        log("可能有上一次遗留的合成服务还在跑，或端口被占用。"
            f"可用 TTS_SERVER_PORT 换一个端口。")
        return 2
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
