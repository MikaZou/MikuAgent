"""远程服务层（路线 A 的 PC 端）。

把现有 backend 的能力通过 WebSocket 暴露给手机等外部客户端，
**不改动 PC 端桌宠的任何行为** —— 它只是在同一个进程里多起一个后台线程。

职责分工（见 docs/TECHNICAL.md 5.4.5 路线 A）：

    手机（只做渲染 + 采集）                 PC（现有代码几乎不动）
    ┌────────────────┐                   ┌──────────────────────────┐
    │ Live2D 渲染     │◀── WebSocket ──▶  │ backend/ 全部复用         │
    │ 麦克风 / 摄像头  │    （局域网）      │  · DeepSeek Agent        │
    │ 音频播放        │                   │  · 记忆 SQLite           │
    └────────────────┘                   │  · TTS / STT provider    │
                                          └──────────────────────────┘

协议（JSON over WebSocket，二进制用 base64）：

    客户端 → 服务端
      {"type":"chat",  "text":"…", "image":"<base64 jpeg，可选>"}
      {"type":"audio", "data":"<base64 wav>", "rate":16000}
      {"type":"ping"}

    服务端 → 客户端
      {"type":"ready",      "provider":{…}}
      {"type":"transcript", "text":"…"}          STT 结果
      {"type":"reply",      "text":"…", "emotion":"…"}
      {"type":"speech",     "data":"<base64 wav>", "duration":1.23}
      {"type":"error",      "message":"…"}
      {"type":"pong"}

设计要点：
  * 重活（LLM / TTS / STT 都是阻塞调用）走 ``asyncio.to_thread``，
    绝不阻塞事件循环；
  * 一路连接串行处理一条消息，避免同一会话并发写 SQLite；
  * 整个服务是**可选**的，起不来只打日志，不影响桌宠。
"""
from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from pathlib import Path
from typing import Optional

from aiohttp import WSMsgType, web

import config

WEB_DIR = config.BASE_DIR / "web"
# 手机端拉取的模型目录。刻意用 REMOTE_MODEL_DIR 而不是 MODEL_PATH.parent：
# 手机端可以指向新模型（moc3 v5），而 PC 桌宠继续用旧模型，互不影响。
MODEL_DIR = config.REMOTE_MODEL_DIR


def _log(msg: str) -> None:
    """打日志：控制台 **和** data/remote.log 都要写。

    为什么必须落文件：`start.bat` 用的是 `pythonw.exe`（没有控制台），
    `print` 出来的东西用户完全看不到。

    更要紧的是：对话/转写/语音这些**交互**日志原先只走 print 不落盘，
    结果 `data/remote.log` 里 65 行全是启动播报、「手机端到底有没有成功
    对话过」完全无从判断（实测交互记录 0 条，而手机其实已经聊上了）。
    排查手机端问题时，这个文件是第一手证据，必须完整。
    """
    print(f"[Remote] {msg}", flush=True)
    try:
        path = Path(config.DATA_DIR) / "remote.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except Exception:  # noqa: BLE001
        # 落盘失败绝不能影响服务本身
        pass


def _local_ips() -> list[str]:
    """列出本机局域网 IP，方便用户在手机上输地址。"""
    import socket

    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))       # 不会真的发包，只为拿到出口网卡
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:  # noqa: BLE001
        pass
    return ips


class RemoteServer:
    """把 agent / tts / stt 暴露成 WebSocket 服务。

    在独立线程里跑自己的 asyncio 事件循环，与 Qt 主线程互不干扰。
    """

    def __init__(self, agent, tts, stt, memory, on_ready=None) -> None:
        self.agent = agent
        self.tts = tts
        self.stt = stt
        self.memory = memory
        self.port = config.REMOTE_PORT
        # 服务就绪后回调（在服务线程里调用，UI 侧需自行切回主线程）
        self.on_ready = on_ready
        self.urls: list[str] = []
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._runner: Optional[web.AppRunner] = None
        self._clients = 0
        self._log_path = Path(config.DATA_DIR) / "remote.log"

    def _write_log(self, msg: str) -> None:
        """写日志文件。

        为什么需要：``start.bat`` 用的是 ``pythonw.exe``（无控制台），
        ``print`` 出来的东西用户完全看不到，包括「手机该打开哪个地址」。
        所以关键信息必须落文件 + 走回调给 UI。
        """
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{timestamp}  {msg}\n")
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 路由
    def _make_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/ws", self._handle_ws)
        app.router.add_get("/health", self._handle_health)
        # ⚠️ 必须注册在 add_static("/model/") **之前**：aiohttp 按注册顺序匹配，
        # 排在后面的话 /model/manifest 会被静态处理器接走，变成 404。
        app.router.add_get("/model/manifest", self._handle_manifest)
        # 手机端要用的静态资源
        app.router.add_static("/static/", WEB_DIR, show_index=False)
        app.router.add_static("/model/", MODEL_DIR, show_index=False)
        return app

    @staticmethod
    def _scan_model_dir() -> list[dict]:
        """列出模型目录里的文件：相对路径、字节数、sha1。

        为什么需要哈希而不是「文件存在就跳过」：手机端下载中断会留下**半截
        文件**，只判断存在会把坏文件当成好的，渲染时才发现模型缺贴图。
        """
        import hashlib

        items: list[dict] = []
        try:
            paths = sorted(p for p in MODEL_DIR.rglob("*") if p.is_file())
        except OSError as exc:  # noqa: BLE001
            _log(f"扫描模型目录失败：{exc}")
            return items

        for path in paths:
            try:
                digest = hashlib.sha1()
                size = 0
                with path.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        digest.update(chunk)
                        size += len(chunk)
            except OSError:
                continue
            items.append({
                # 统一用 posix 分隔符，手机端直接拼 URL 即可
                "path": path.relative_to(MODEL_DIR).as_posix(),
                "size": size,
                "sha1": digest.hexdigest(),
            })
        return items

    async def _handle_manifest(self, request: web.Request) -> web.Response:
        """模型清单，供手机端增量同步与完整性校验。"""
        files = await asyncio.to_thread(self._scan_model_dir)
        total = sum(f["size"] for f in files)
        return web.json_response({
            "root": MODEL_DIR.name,
            "count": len(files),
            "total": total,
            "files": files,
        })

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "ok": True,
            "provider": self.provider_info(),
            "clients": self._clients,
        })

    async def _handle_index(self, request: web.Request) -> web.Response:
        page = WEB_DIR / "phone.html"
        if not page.exists():
            return web.Response(status=404, text="web/phone.html 不存在")
        # 页面里用 __CDN__ 占位，便于换成国内可用的镜像
        html = page.read_text(encoding="utf-8").replace("__CDN__", config.REMOTE_CDN)
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    def provider_info(self) -> dict:
        return {
            "llm": config.DEEPSEEK_MODEL,
            "tts": config.TTS_ENGINE,
            "stt": config.STT_TRANSCRIBER,
            # 注意：别写成 `config.VISION_ENABLED or True` —— `x or True` 恒为 True，
            # 那样不管用户有没有开视频对话，这里永远报 true（踩过）。
            "vision": bool(config.VISION_ENABLED),
            "voice_id": config.MINIMAX_VOICE_ID or "",
        }

    # -------------------------------------------------------------- WebSocket
    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        # compress=False 是**必须**的：aiohttp 的 WebSocketResponse 默认
        # compress=True，会在握手时广告 permessage-deflate 扩展。而 Android 端
        # 用的 OkHttp 并不实现这个扩展，两边协商不一致时服务端会把带 RSV1 位的
        # 帧当成非法帧，报「Received frame with non-zero reserved bits」并断开。
        # 症状很隐蔽：手机一发语音（205KB）连接就断，文字聊天却完全正常。
        ws = web.WebSocketResponse(
            heartbeat=30,
            max_msg_size=32 * 1024 * 1024,
            compress=False,
        )
        await ws.prepare(request)
        self._clients += 1
        peer = request.remote
        _log(f"客户端接入 {peer}（当前 {self._clients} 个）")
        try:
            await ws.send_json({"type": "ready", "provider": self.provider_info()})
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._on_message(ws, msg.data, peer)
                elif msg.type == WSMsgType.ERROR:
                    _log(f"{peer} 连接异常：{ws.exception()}")
                    break
        finally:
            self._clients -= 1
            _log(f"客户端断开 {peer}（剩余 {self._clients} 个）")
        return ws

    async def _on_message(self, ws: web.WebSocketResponse, raw: str, peer: str) -> None:
        try:
            req = json.loads(raw)
        except Exception:  # noqa: BLE001
            await ws.send_json({"type": "error", "message": "不是合法 JSON"})
            return

        kind = req.get("type")
        try:
            if kind == "ping":
                await ws.send_json({"type": "pong"})
            elif kind == "chat":
                await self._do_chat(ws, req, peer)
            elif kind == "audio":
                await self._do_audio(ws, req, peer)
            else:
                await ws.send_json({"type": "error", "message": f"未知类型 {kind}"})
        except Exception as exc:  # noqa: BLE001
            _log(f"处理 {kind} 出错：{exc}")
            try:
                await ws.send_json({"type": "error", "message": str(exc)})
            except Exception:  # noqa: BLE001
                pass

    async def _do_chat(self, ws, req: dict, peer: str) -> None:
        text = (req.get("text") or "").strip()
        if not text:
            await ws.send_json({"type": "error", "message": "空文本"})
            return

        image: Optional[bytes] = None
        if req.get("image"):
            try:
                image = base64.b64decode(req["image"])
            except Exception:  # noqa: BLE001
                await ws.send_json({"type": "error", "message": "图片解码失败"})
                return

        t0 = time.time()
        # agent / tts 都是阻塞调用，扔到线程池，别堵事件循环
        result = await asyncio.to_thread(
            self.agent.chat, req.get("session_id"), text, image
        )
        _log(f"{peer} 对话 {time.time()-t0:.2f}s "
             f"(带图={bool(image)}) -> {result.get('emotion')}")

        await ws.send_json({
            "type": "reply",
            "text": result.get("reply", ""),
            "emotion": result.get("emotion", "NORMAL"),
            "session_id": result.get("session_id"),
        })

        # 语音单独下发，手机端可以先显示文字再等音频
        if config.TTS_ENABLED and result.get("reply"):
            await self._send_speech(ws, result["reply"], result.get("emotion", "NORMAL"))

    async def _send_speech(self, ws, text: str, emotion: str) -> None:
        t0 = time.time()
        out = await asyncio.to_thread(self.tts.synthesize, text, emotion)
        if not out:
            _log("合成失败，跳过语音")
            return
        path, duration = out
        data = await asyncio.to_thread(Path(path).read_bytes)
        _log(f"语音 {duration:.2f}s / 合成 {time.time()-t0:.2f}s / {len(data)/1024:.0f}KB")
        await ws.send_json({
            "type": "speech",
            "format": "wav",
            "duration": float(duration),
            "data": base64.b64encode(data).decode("ascii"),
        })

    async def _do_audio(self, ws, req: dict, peer: str) -> None:
        """手机端按住说话：把录音传上来转写，然后走正常对话。"""
        raw = req.get("data") or ""
        if not raw:
            await ws.send_json({"type": "error", "message": "空音频"})
            return
        try:
            wav_bytes = base64.b64decode(raw)
        except Exception:  # noqa: BLE001
            await ws.send_json({"type": "error", "message": "音频解码失败"})
            return

        t0 = time.time()
        text = await asyncio.to_thread(self._transcribe_wav, wav_bytes)
        _log(f"{peer} 语音转写 {time.time()-t0:.2f}s -> {text[:30]!r}")
        if not text:
            await ws.send_json({"type": "transcript", "text": ""})
            return
        await ws.send_json({"type": "transcript", "text": text})
        # 转写完直接当成一次对话
        await self._do_chat(ws, {"text": text, "session_id": req.get("session_id")}, peer)

    def _transcribe_wav(self, wav_bytes: bytes) -> str:
        """把手机传来的 WAV 交给配置好的转写通道。"""
        import io

        import numpy as np
        import soundfile as sf

        data, rate = sf.read(io.BytesIO(wav_bytes), always_2d=True)
        mono = data.mean(axis=1).astype("float32")
        if rate != self.stt.sample_rate:
            # soundfile 不重采样，必须自己转，否则音调会不对（这个坑踩过）
            import av

            rs = av.AudioResampler(format="fltp", layout="mono", rate=self.stt.sample_rate)
            fr = av.AudioFrame.from_ndarray(
                mono.reshape(1, -1), format="flt", layout="mono"
            )
            fr.sample_rate = rate
            parts = [o.to_ndarray().reshape(-1) for o in rs.resample(fr)]
            parts += [o.to_ndarray().reshape(-1) for o in rs.resample(None)]
            mono = np.concatenate(parts) if parts else mono
        return self.stt._transcribe(mono)

    # -------------------------------------------------------------- 生命周期
    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._runner = web.AppRunner(self._make_app())
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        try:
            await site.start()
        except OSError as exc:
            _log(f"端口 {self.port} 起不来（可能被占用）：{exc}")
            return
        ips = _local_ips()
        self.urls = [f"http://{ip}:{self.port}/" for ip in ips]
        _log(f"已启动，监听 0.0.0.0:{self.port}")
        self._write_log(f"远程服务已启动，监听 0.0.0.0:{self.port}")
        for url in self.urls:
            _log(f"  手机浏览器打开： {url}")
            self._write_log(f"  手机浏览器打开： {url}")
        if not self.urls:
            _log("  （没探测到局域网 IP，请用 ipconfig 查看）")
            self._write_log("  （没探测到局域网 IP，请用 ipconfig 查看）")

        # 通知 UI（服务线程里回调，UI 侧负责切回主线程）
        if self.on_ready is not None:
            try:
                self.on_ready(self.urls, self.provider_info())
            except Exception as exc:  # noqa: BLE001
                _log(f"on_ready 回调失败：{exc}")

        # 必须在这里挂住：asyncio.run() 会在协程返回后立刻关闭事件循环，
        # 那样服务刚 start() 就被拆掉了（表现为日志说已启动、实际连不上）。
        await self._stop_event.wait()

    def start(self) -> bool:
        """在后台线程里启动服务。返回是否成功拉起线程（不代表端口一定可用）。"""
        if not config.REMOTE_ENABLED:
            _log("已在配置里关闭（REMOTE_ENABLED=false）")
            return False

        def run() -> None:
            try:
                asyncio.run(self._serve())
            except Exception as exc:  # noqa: BLE001
                _log(f"服务异常退出：{exc}")

        self._thread = threading.Thread(target=run, daemon=True, name="remote-server")
        self._thread.start()
        return True

    def stop(self) -> None:
        loop, runner = self._loop, self._runner
        if loop is None or runner is None:
            return
        ev = getattr(self, "_stop_event", None)
        if ev is not None:
            try:
                loop.call_soon_threadsafe(ev.set)
            except Exception:  # noqa: BLE001
                pass
        try:
            fut = asyncio.run_coroutine_threadsafe(runner.cleanup(), loop)
            fut.result(timeout=5)
        except Exception:  # noqa: BLE001
            pass


class RemoteController:
    """按 config 管理远程服务的启停，支持**运行中**改配置。

    以前 ``REMOTE_ENABLED`` 只在启动时读一次，用户在设置面板里改了开关
    必须重启程序才生效 —— 现在改完立刻调 ``sync()`` 就行。

    重启时必须等旧线程真的退出（端口释放）再 bind，否则会撞
    「端口被占用」而静默失败。
    """

    def __init__(self, agent, tts, stt, memory, on_ready=None) -> None:
        self._agent = agent
        self._tts = tts
        self._stt = stt
        self._memory = memory
        self._on_ready = on_ready
        self._server = None
        self._port = None
        self._lock = threading.Lock()

    @property
    def server(self):
        return self._server

    @property
    def running(self) -> bool:
        return self._server is not None

    def sync(self) -> str:
        """让服务状态与当前 config 一致。

        返回 started / stopped / restarted / unchanged / failed。
        """
        with self._lock:
            want = bool(config.REMOTE_ENABLED)
            port = int(config.REMOTE_PORT)

            if self._server is not None and not want:
                self._stop_locked()
                return "stopped"

            if self._server is None:
                if not want:
                    return "unchanged"
                return "started" if self._start_locked(port) else "failed"

            if self._port != port:
                self._stop_locked()
                return "restarted" if self._start_locked(port) else "failed"

            return "unchanged"

    def _start_locked(self, port: int) -> bool:
        server = RemoteServer(
            self._agent, self._tts, self._stt, self._memory, on_ready=self._on_ready
        )
        if not server.start():
            return False
        self._server = server
        self._port = port
        return True

    def _stop_locked(self) -> None:
        server = self._server
        self._server = None
        self._port = None
        if server is None:
            return
        try:
            server.stop()
            thread = getattr(server, "_thread", None)
            if thread is not None:
                # 等端口真的释放；不等的话紧接着的重启会 bind 失败
                thread.join(timeout=6)
        except Exception as exc:  # noqa: BLE001
            _log(f"停止远程服务出错：{exc}")

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()
