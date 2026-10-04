"""把 PC 的 `.env` 里的 API 配置推到手机（等价于在设置面板里粘贴一次）。

手机端能独立运行之后，它需要自己的 DeepSeek / MiniMax 凭据。手打一串 40 多位
的 Key 很容易出错，所以支持「从 PC 复制」：PC 设置窗口那个「复制 API 配置」按钮
产出的就是同一段 JSON，这个脚本是它的**自动化版本**（同时也是 Phase 5 的验收工具）。

安全约定：**Key 绝不出现在命令行参数或输出里**。脚本自己读 `.env`、
自己拼 JSON，只打印「写了哪些字段」。这样终端历史、日志、截图都不会漏 Key。

用法：
    .venv\\Scripts\\python.exe tools/phone_push_config.py            # 推给真机
    .venv\\Scripts\\python.exe tools/phone_push_config.py --show    # 只列出字段名
"""
from __future__ import annotations

import base64
import asyncio
import json
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "backend"))

import envfile  # noqa: E402

#: 与 `BrainConfig.importJson` 认的字段一一对应（也就是 PC .env 的键名）
FIELDS = [
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_TEMPERATURE",
    "DEEPSEEK_THINKING",
    "MINIMAX_API_KEY",
    "MINIMAX_BASE_URL",
    "MINIMAX_TTS_MODEL",
    "MINIMAX_VOICE_ID",
    "MINIMAX_SPEED",
    "MINIMAX_ASR_MODEL",
    "STT_LANGUAGE",
    "VISION_DETAIL",
    "MAX_HISTORY_MESSAGES",
]


def payload() -> dict[str, str]:
    """只带**非空**的字段。

    空值不传：`importJson` 对「字段在、值为空串」的语义是「清空」，
    而 PC 的 .env 里有大量注释掉/留空的键。不过滤的话，手机上刚填好的
    Key 会被下一次推送抹掉。
    """
    env = envfile.read_env()
    return {k: env[k].strip() for k in FIELDS if env.get(k, "").strip()}


def adb(*args: str) -> str:
    out = subprocess.run(["adb", *args], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    return (out.stdout or "") + (out.stderr or "")


async def push(js_payload_b64: str) -> int:
    import aiohttp

    pid = adb("shell", "pidof", "com.mikuagent.pet").strip().split()
    if not pid:
        print("应用没在跑：adb shell pidof com.mikuagent.pet 是空的")
        return 1
    adb("forward", "tcp:9222", f"localabstract:webview_devtools_remote_{pid[0]}")

    async with aiohttp.ClientSession() as session:
        async with session.get("http://127.0.0.1:9222/json") as resp:
            pages = json.loads(await resp.text())
        target = next((p for p in pages if "index.html" in (p.get("url") or "")), None)
        if target is None:
            print("找不到 WebView 页面")
            return 1
        async with session.ws_connect(target["webSocketDebuggerUrl"]) as ws:
            # 走 atob 解 base64：Key 不进 JS 源码，也就不会被 CDP 日志或页面错误堆栈带出来
            expr = (
                "JSON.stringify(native.saveApiConfig("
                f"decodeURIComponent(escape(atob('{js_payload_b64}')))))"
            )
            await ws.send_json({
                "id": 1, "method": "Runtime.evaluate",
                "params": {"expression": expr, "returnByValue": True, "awaitPromise": True},
            })
            while True:
                msg = await ws.receive_json()
                if msg.get("id") != 1:
                    continue
                result = msg.get("result", {})
                if "exceptionDetails" in result:
                    print("异常：" + json.dumps(result["exceptionDetails"], ensure_ascii=False))
                    return 1
                print("手机已保存字段：" + str(result.get("result", {}).get("value")))
                return 0


def main() -> int:
    data = payload()
    if not data:
        print("`.env` 里没有可用的 API 配置")
        return 1
    print(f"从 .env 取到 {len(data)} 个字段：" + ", ".join(sorted(data)))
    if "--show" in sys.argv:
        return 0

    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    b64 = base64.b64encode(raw).decode("ascii")
    return asyncio.run(push(b64))


if __name__ == "__main__":
    raise SystemExit(main())
