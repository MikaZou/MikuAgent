"""视觉 / 视频对话诊断与回归工具。

用法::

    python tools/diag_vision.py camera     # 探测摄像头并抓一帧存下来看
    python tools/diag_vision.py api        # 验证 DeepSeek 视觉 API 确实生效
    python tools/diag_vision.py pipeline   # 用合成画面跑通整条链路（不需要摄像头）
    python tools/diag_vision.py all

``pipeline`` 是**不需要摄像头**的端到端验证：造一张已知内容的图，
直接喂给 ``MikuAgent.chat(image=...)``，检查回复是否真的描述了画面内容。
在没有摄像头（或摄像头打不开）的机器上，这是唯一能验证代码链路的方式。
"""
from __future__ import annotations

import io
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "backend"))

import config  # noqa: E402

OUT_DIR = BASE / ".diag"


def _banner(title: str) -> None:
    print()
    print("=" * 68)
    print(title)
    print("=" * 68)


# ------------------------------------------------------------------ 合成画面
def make_fake_portrait() -> bytes:
    """造一张「人像」：浅色背景 + 圆脸 + 两只眼睛 + 微笑的嘴 + 一点红色。"""
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (480, 480), (250, 240, 220))
    d = ImageDraw.Draw(im)
    d.ellipse((120, 100, 360, 340), fill=(240, 200, 160))        # 脸
    d.ellipse((170, 180, 210, 220), fill=(60, 40, 30))           # 左眼
    d.ellipse((270, 180, 310, 220), fill=(60, 40, 30))           # 右眼
    d.arc((180, 230, 300, 300), 20, 160, fill=(150, 60, 60), width=8)  # 微笑
    d.rectangle((200, 360, 280, 430), fill=(230, 60, 60))        # 红色衣服
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def make_color_shape(color: tuple[int, int, int], shape: str) -> bytes:
    """造一张「纯色 + 几何形状」的图，用于有区分度地验证视觉是否真的生效。"""
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (256, 256), (255, 255, 255))
    d = ImageDraw.Draw(im)
    box = (40, 40, 216, 216)
    if shape == "circle":
        d.ellipse(box, fill=color)
    else:
        d.rectangle(box, fill=color)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------- 摄像头
def cmd_camera() -> int:
    _banner("摄像头探测")
    from ui.capture import CameraSession, camera_available, probe_cameras

    print(f"  opencv 可用: {camera_available()}")
    if not camera_available():
        print("  → 未安装 opencv-python-headless，摄像头路径不可用")
        print("    （截屏与剪贴板路径仍然可用，无需任何额外依赖）")
        return 1

    t0 = time.time()
    found = probe_cameras(4)
    print(f"  探测索引 0~3: {found if found else '（都不行）'}   耗时 {time.time()-t0:.1f}s")
    if not found:
        print("  → 摄像头打不开。常见原因：被其他程序占用 / 系统未授权 / 无设备")
        return 1

    idx = found[0]
    session = CameraSession(idx)
    ok, reason = session.open()
    print(f"  open(索引 {idx}): {ok} {reason}")
    if not ok:
        return 1

    t1 = time.time()
    image = session.read_frame()
    print(f"  读帧: {image.width()}x{image.height()}  耗时 {time.time()-t1:.3f}s" if image
          else "  读帧: 失败")
    t2 = time.time()
    jpeg = session.read_jpeg()
    session.close()
    if not jpeg:
        print("  JPEG 编码失败")
        return 1
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / "camera_frame.jpg"
    path.write_bytes(jpeg)
    print(f"  JPEG: {len(jpeg)/1024:.1f} KB  编码耗时 {time.time()-t2:.3f}s")
    print(f"  → 已存 {path}（看一眼画面对不对）")
    return 0


# ------------------------------------------------------------------ API 视觉
def cmd_api() -> int:
    _banner("DeepSeek 视觉 API 验证")
    import base64

    from openai import OpenAI

    if not config.HAS_API_KEY:
        print("  未配置 API Key")
        return 1
    client = OpenAI(api_key=config.DEEPSEEK_API_KEY, base_url=config.DEEPSEEK_BASE_URL)
    print(f"  model = {config.DEEPSEEK_MODEL}   thinking = {config.DEEPSEEK_THINKING}")

    cases = [
        ((0, 0, 255), "circle", "蓝色圆形"),
        ((0, 180, 0), "rectangle", "绿色正方形"),
        ((255, 220, 0), "circle", "黄色圆形"),
    ]
    kw: dict = {"model": config.DEEPSEEK_MODEL, "temperature": 0.2}
    if config.DEEPSEEK_THINKING in ("enabled", "disabled"):
        kw["extra_body"] = {"thinking": {"type": config.DEEPSEEK_THINKING}}

    passed = 0
    for color, shape, expect in cases:
        raw = make_color_shape(color, shape)
        url = "data:image/png;base64," + base64.b64encode(raw).decode()
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "图里是什么颜色的什么形状？只回答「颜色+形状」。"},
            {"type": "image_url", "image_url": {"url": url, "detail": "low"}},
        ]}]
        try:
            r = client.chat.completions.create(messages=messages, max_tokens=200, **kw)
            answer = (r.choices[0].message.content or "").strip()
            ok = bool(answer)
            passed += 1 if ok else 0
            print(f"  期望 {expect:<10} → {answer[:24]!r:<28} {'✓' if ok else '（空，检查 thinking 设置）'}")
        except Exception as exc:  # noqa: BLE001
            print(f"  期望 {expect:<10} → 失败 {type(exc).__name__}: {str(exc)[:90]}")

    # token 成本对照：同提示词，带图 vs 不带图
    try:
        plain = client.chat.completions.create(
            model=config.DEEPSEEK_MODEL, temperature=0.2,
            messages=[{"role": "user", "content": "图里是什么颜色的什么形状？只回答「颜色+形状」。"}],
            max_tokens=60, **{k: v for k, v in kw.items() if k == "extra_body"},
        )
        raw = make_color_shape((0, 0, 255), "circle")
        url = "data:image/png;base64," + base64.b64encode(raw).decode()
        withimg = client.chat.completions.create(
            model=config.DEEPSEEK_MODEL, temperature=0.2,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": "图里是什么颜色的什么形状？只回答「颜色+形状」。"},
                {"type": "image_url", "image_url": {"url": url, "detail": "low"}},
            ]}], max_tokens=60, **{k: v for k, v in kw.items() if k == "extra_body"},
        )
        delta = withimg.usage.prompt_tokens - plain.usage.prompt_tokens
        print(f"\n  图片 token 成本（detail=low）: 约 {delta} tokens"
              f"  ({plain.usage.prompt_tokens} → {withimg.usage.prompt_tokens})")
    except Exception as exc:  # noqa: BLE001
        print(f"\n  token 成本测量失败: {exc}")

    print(f"\n  结果: {passed}/{len(cases)} 通过")
    return 0 if passed == len(cases) else 1


# --------------------------------------------------------------- 整条链路
def cmd_pipeline() -> int:
    _banner("端到端链路（合成画面，不需要摄像头）")
    from agent import MikuAgent
    from memory import MemoryStore

    store = MemoryStore(config.DB_PATH)
    agent = MikuAgent(store)
    print(f"  live = {agent.live}   model = {config.DEEPSEEK_MODEL}")
    if not agent.live:
        print("  → 离线演示模式，无法验证视觉（需要 API Key）")
        return 1

    frame = make_fake_portrait()
    print(f"  合成画面: {len(frame)/1024:.1f} KB JPEG（圆脸 + 眼睛 + 微笑 + 红色衣服）")

    prompt = "（这是摄像头看到的画面）你看看我现在什么样？"
    t0 = time.time()
    result = agent.chat(None, prompt, frame)
    elapsed = time.time() - t0

    reply = result.get("reply", "")
    print(f"\n  {elapsed:.2f}s  emotion={result.get('emotion')}  had_image={result.get('had_image')}")
    print(f"  回复: {reply}")
    ok = bool(reply.strip()) and result.get("had_image")
    print(f"\n  正文非空 + 标记带图: {'✓' if ok else '✗'}")

    # 历史里必须只留占位符，不能把 base64 存进去
    history = store.get_messages(result.get("session_id"), limit=5)
    user_rows = [m for m in history if m.get("role") == "user"]
    if user_rows:
        stored = user_rows[-1].get("content", "")
        placeholder_ok = stored.endswith("[图片]") and len(stored) < 500
        print(f"  历史占位符: {stored[:40]!r}  {'✓' if placeholder_ok else '✗ 图片可能被写进库了'}")
        ok = ok and placeholder_ok

    print(f"\n  结果: {'通过' if ok else '未通过'}")
    return 0 if ok else 1


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    print(f"config: VISION_DETAIL={config.VISION_DETAIL} MAX_SIDE={config.VISION_MAX_SIDE} "
          f"QUALITY={config.VISION_JPEG_QUALITY} CAMERA_INDEX={config.VISION_CAMERA_INDEX}")

    results: list[tuple[str, int]] = []
    if which in ("all", "camera"):
        results.append(("camera", cmd_camera()))
    if which in ("all", "api"):
        results.append(("api", cmd_api()))
    if which in ("all", "pipeline"):
        results.append(("pipeline", cmd_pipeline()))

    _banner("汇总")
    for name, code in results:
        print(f"  {name:<10} {'通过' if code == 0 else '未通过'}")
    return 0 if all(c == 0 for _, c in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
