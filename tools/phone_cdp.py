"""在真机上直接对 WebView 里的页面求值（调试用）。

为什么需要：手机端的模型取景/表情全在 WebView 的 JS 里，而 MIUI 的 logcat
对第三方应用的日志很不稳定（同一个 tag 前一次能看到、下一次整段消失），
靠 `adb logcat` 排查等于掷骰子。走 Chrome DevTools Protocol 直接问页面，
拿到的就是**当下真实**的变量值。

用法：
    .venv\\Scripts\\python.exe tools/phone_cdp.py "JSON.stringify(artBox)"
    .venv\\Scripts\\python.exe tools/phone_cdp.py "measureArtBounds()" --raw
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys

import aiohttp

TAG = "MikuAgent"


def adb(*args: str) -> str:
    out = subprocess.run(["adb", *args], capture_output=True, text=True, encoding="utf-8",
                         errors="replace")
    return (out.stdout or "") + (out.stderr or "")


def webview_pid() -> str:
    text = adb("shell", "pidof", "com.mikuagent.pet")
    pid = text.strip().split()[0] if text.strip() else ""
    if not pid:
        raise SystemExit("应用没在跑：adb shell pidof com.mikuagent.pet 是空的")
    return pid


async def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    expression = args[0]
    pid = webview_pid()
    socket = f"webview_devtools_remote_{pid}"
    adb("forward", "tcp:9222", f"localabstract:{socket}")

    async with aiohttp.ClientSession() as session:
        async with session.get("http://127.0.0.1:9222/json") as resp:
            pages = json.loads(await resp.text())
        target = None
        for page in pages:
            if "index.html" in (page.get("url") or ""):
                target = page
                break
        target = target or (pages[0] if pages else None)
        if target is None:
            print("找不到 WebView 页面")
            return 1
        ws_url = target["webSocketDebuggerUrl"]
        print(f"[cdp] {target.get('url')}")

        async with session.ws_connect(ws_url) as ws:
            await ws.send_json({
                "id": 1, "method": "Runtime.evaluate",
                "params": {"expression": expression, "returnByValue": True, "awaitPromise": True},
            })
            while True:
                msg = await ws.receive_json()
                if msg.get("id") != 1:
                    continue
                result = msg.get("result", {})
                if "exceptionDetails" in result:
                    print("异常：" + json.dumps(result["exceptionDetails"], ensure_ascii=False))
                    return 1
                value = result.get("result", {}).get("value")
                print(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
                return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
