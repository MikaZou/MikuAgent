"""回归：远程服务的多模型路由。

为什么要有它：手机端现在按**模型 id** 拉文件（/model/<id>/...），
而「用哪个模型」可以在 PC 控制台上随时改。这条链路一旦错了，
手机端的表现是「模型同步失败 / 渲染空白」，离真正的原因很远。
这里不起 Qt、不起真服务，直接用 aiohttp 的测试客户端把路由打一遍。

用法：.venv\\Scripts\\python.exe tools\\test_models_api.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "backend"))

import models_catalog  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from remote_server import RemoteServer  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    # 不用 ✅/❌：Windows 控制台默认是 GBK，emoji 会让脚本自己抛
    # UnicodeEncodeError（比测试失败还误导）。
    print(f"  [{'ok' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(name)


async def run() -> int:
    server = RemoteServer(None, None, None, None)
    client = TestClient(TestServer(server._make_app()))
    await client.start_server()

    # ---- 1) 模型清单 ----
    resp = await client.get("/model/list")
    body = await resp.json()
    ids = [m["id"] for m in body.get("models", [])]
    check("GET /model/list", resp.status == 200 and "miku_v5" in ids, f"ids={ids}")
    check("清单带 present 标记",
          all("present" in m for m in body.get("models", [])),
          str([(m["id"], m["present"]) for m in body.get("models", [])]))
    check("清单带 phone_model", body.get("phone_model") in ids, body.get("phone_model"))

    # ---- 2) 两个模型各自的 manifest ----
    for mid in ("miku", "miku_v5"):
        resp = await client.get(f"/model/{mid}/manifest")
        body = await resp.json()
        files = {f["path"] for f in body.get("files", [])}
        check(f"GET /model/{mid}/manifest", resp.status == 200 and body.get("id") == mid,
              f"{body.get('count')} 个文件 / {body.get('total', 0) / 1048576:.1f}MB")
        check(f"{mid} 的 model3.json 在清单里", body.get("model3") in files, body.get("model3"))
        check(f"{mid} 带 profile", bool(body.get("profile")),
              json.dumps({k: body.get("profile", {}).get(k)
                          for k in ("watermark_param", "manual_breath")}, ensure_ascii=False))

    # profile 必须真的按模型不同 —— 这是「两端行为一致」的关键
    p4 = (await (await client.get("/model/miku/manifest")).json())["profile"]
    p5 = (await (await client.get("/model/miku_v5/manifest")).json())["profile"]
    check("两个模型的 profile 不同", p4 != p5,
          f"v4 水印={p4.get('watermark_param')} / v5 水印={p5.get('watermark_param')}")

    # ---- 3) 兼容老 APK 的写法 ----
    resp = await client.get("/model/manifest")
    body = await resp.json()
    check("GET /model/manifest（老写法）跟随 phone_model",
          resp.status == 200 and body.get("id") == models_catalog.selected("phone"),
          f"id={body.get('id')}")

    # ---- 4) 模型文件 ----
    resp = await client.get("/model/miku_v5/miku.model3.json")
    data = await resp.read()
    check("取模型文件", resp.status == 200 and b"FileReferences" in data,
          f"{len(data)} 字节")
    resp = await client.get("/model/miku/表情和动作/07_点头.motion3.json")
    check("取中文路径文件", resp.status == 200, f"{resp.status}")

    # ---- 5) 目录穿越必须被拦住 ----
    for bad in ("/model/miku_v5/../../config.py",
                "/model/miku_v5/..%2F..%2Fconfig.py",
                "/model/miku_v5/../../../.env",
                "/model/../../config.py",
                "/model/..%2F..%2Fconfig.py"):
        resp = await client.get(bad)
        check(f"拒绝穿越 {bad}", resp.status in (404, 403), f"status={resp.status}")

    # ---- 5b) 旧版网页 phone.html 的老写法（不带模型 id）也要能取到文件 ----
    # 已经装在手机/浏览器里的旧页面写的是 /model/miku.model3.json，
    # 以及贴图 /model/miku.4096/texture_00.png（第一段不是模型 id）。
    resp = await client.get("/model/miku.model3.json")
    check("老写法取 model3.json（跟随手机端选择）",
          resp.status == 200 and b"FileReferences" in await resp.read(), f"status={resp.status}")
    # 中文子目录（表情和动作/）只有经典模型有，所以要先把手机端切到经典。
    # 这同时验证了「老写法是从**手机端当前选中的目录**里取的」。
    prev = models_catalog.selected("phone")
    models_catalog.select("phone", "miku")
    resp = await client.get("/model/%E8%A1%A8%E6%83%85%E5%92%8C%E5%8A%A8%E4%BD%9C/07_%E7%82%B9%E5%A4%B4.motion3.json")
    check("老写法取中文子目录文件", resp.status == 200, f"status={resp.status}")
    models_catalog.select("phone", prev)

    # ---- 6) 未知模型 ----
    resp = await client.get("/model/nope/manifest")
    check("未知模型 → 404", resp.status == 404, f"status={resp.status}")

    # ---- 7) health ----
    resp = await client.get("/health")
    body = await resp.json()
    check("GET /health", resp.status == 200 and body.get("ok") is True)

    # ---- 8) WebSocket：ready 带模型清单；set_model 只**记录**手机的选择 ----
    #
    # 手机端是「自己用哪个模型」的主人：换模型由手机自己切（本地改 + 重同步），
    # 发 set_model 只是把选择告诉 PC 记一笔，**PC 不再广播回去** ——
    # 广播会把这台手机的选择强加到别的手机上。
    original = models_catalog.selected("phone")
    ws = await client.ws_connect("/ws")
    hello = await ws.receive_json()
    check("WS ready 带 models 清单",
          hello.get("type") == "ready" and bool(hello.get("models")),
          f"phone_model={hello.get('phone_model')} models={len(hello.get('models') or [])}")

    target = "miku" if hello.get("phone_model") != "miku" else "miku_v5"
    await ws.send_json({"type": "set_model", "id": target})
    # set_model 没有回音，所以只能轮询等服务端处理完 —— 直接断言会撞上竞态
    # （之前那条 await receive_json() 顺手充当了同步点，去掉之后就露出来了）。
    deadline = time.time() + 3.0
    while time.time() < deadline and models_catalog.selected("phone") != target:
        await asyncio.sleep(0.05)
    check("set_model 写进了 PC 的记录", models_catalog.selected("phone") == target,
          f"期望 {target}，实得 {models_catalog.selected('phone')}")
    resp = await client.get("/model/manifest")
    check("老写法 manifest 跟着记录走", (await resp.json()).get("id") == target)

    # 不该收到任何回音：给它 1.5 秒，只要收到东西就是多广播了
    try:
        extra = await asyncio.wait_for(ws.receive_json(), timeout=1.5)
        check("set_model 不广播回音", False, json.dumps(extra, ensure_ascii=False))
    except asyncio.TimeoutError:
        check("set_model 不广播回音", True, "1.5s 内没有多余消息")

    await ws.send_json({"type": "set_model", "id": "不存在的模型"})
    err = await asyncio.wait_for(ws.receive_json(), timeout=3.0)
    check("未知模型 set_model 被拒", err.get("type") == "error", json.dumps(err, ensure_ascii=False))
    await ws.close()

    # 还原，别把用户的选择改掉
    models_catalog.select("phone", original)

    await client.close()
    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
