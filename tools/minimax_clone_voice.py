"""用 v4c 发布会说话音频在 MiniMax 上克隆初音音色。

流程（对应官方文档 /v1/files/upload + /v1/voice_clone）：

  1. 上传主参考音频（≥10s）purpose=voice_clone  → file_id
  2. 上传示例音频（<8s）  purpose=prompt_audio → prompt_file_id
  3. 调 /v1/voice_clone 得到 voice_id，并要一段试听音频
  4. 试听音频下载后播放，确认音色

⚠️ 两个官方约束：
  * 复刻出的音色是**临时的** —— 168 小时（7 天）内未在任何合成接口使用会被删除
  * 调本接口前需完成**个人或企业认证**，否则返回 2038

用法::

    python tools/minimax_clone_voice.py            # 克隆 + 下载试听
    python tools/minimax_clone_voice.py --play     # 额外播放试听
    python tools/minimax_clone_voice.py --voice-id MyMiku2026
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "backend"))

import config  # noqa: E402

VOICE_DIR = BASE / "assets" / "voice" / "miku_v4c"
MAIN_AUDIO = VOICE_DIR / "miku_v4c_speech_full.wav"      # 56s，≥10s 满足主参考要求
EXAMPLE_AUDIO = VOICE_DIR / "miku_v4c_ref_5s.wav"        # 5.4s，<8s 满足示例要求
EXAMPLE_TEXT = "大家好，我是初音未来。"                    # 与示例音频逐字一致（官方简介确认）
PREVIEW_TEXT = "主人早上好呀，今天想和 Miku 一起做什么呢？"

DEFAULT_VOICE_ID = "MikuV4C2026"
OUT = BASE / ".tmp" / "audio"


def redact(s: str) -> str:
    return (s[:8] + "…" + s[-4:]) if len(s) > 16 else "***"


def upload(path: Path, purpose: str) -> int:
    import requests

    url = f"{config.MINIMAX_BASE_URL}/v1/files/upload"
    headers = {"Authorization": f"Bearer {config.MINIMAX_API_KEY}"}
    files = {"file": (path.name, path.read_bytes(), "audio/wav")}
    data = {"purpose": purpose}
    print(f"  上传 {path.name}  ({path.stat().st_size/1024/1024:.2f} MB)  purpose={purpose}")
    r = requests.post(url, headers=headers, data=data, files=files, timeout=300)
    j = r.json()
    base = j.get("base_resp") or {}
    if r.status_code != 200 or base.get("status_code") not in (0, None):
        raise RuntimeError(f"上传失败 HTTP {r.status_code}: "
                           f"{json.dumps(j, ensure_ascii=False)[:300]}")
    fid = (j.get("file") or {}).get("file_id")
    print(f"    file_id = {fid}  ({j['file'].get('bytes')} 字节)")
    return int(fid)


def clone(file_id: int, prompt_id: int, voice_id: str, with_preview: bool = True) -> dict:
    import requests

    payload: dict = {
        "file_id": file_id,
        "voice_id": voice_id,
        "clone_prompt": {"prompt_audio": prompt_id, "prompt_text": EXAMPLE_TEXT},
        # 我们的人声是 demucs 分离出来的，开这两个能进一步清掉残留
        "need_noise_reduction": True,
        "need_volume_normalization": True,
    }
    if with_preview:
        payload.update({"text": PREVIEW_TEXT, "model": config.MINIMAX_TTS_MODEL})

    url = f"{config.MINIMAX_BASE_URL}/v1/voice_clone"
    headers = {
        "Authorization": f"Bearer {config.MINIMAX_API_KEY}",
        "Content-Type": "application/json",
    }
    print(f"  复刻音色 voice_id={voice_id} …")
    t0 = time.time()
    r = requests.post(url, headers=headers, json=payload, timeout=300)
    dt = time.time() - t0
    j = r.json()
    base = j.get("base_resp") or {}
    code = base.get("status_code")
    print(f"    HTTP {r.status_code}  业务码 {code} ({base.get('status_msg')})  耗时 {dt:.2f}s")

    if code == 2038:
        raise RuntimeError(
            "无复刻权限：需要先在 MiniMax 控制台完成**个人或企业实名认证**，"
            "否则 /v1/voice_clone 会返回 2038。\n"
            "    认证入口：https://platform.minimaxi.com/user-center/basic-information"
        )
    if code not in (0, None):
        raise RuntimeError(f"复刻失败：{json.dumps(j, ensure_ascii=False)[:400]}")
    return j


def download(url: str, dest: Path) -> Path | None:
    import requests

    try:
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return dest
    except Exception as exc:  # noqa: BLE001
        print(f"    试听音频下载失败：{exc}")
        return None


def update_env(voice_id: str) -> None:
    env = BASE / ".env"
    text = env.read_text(encoding="utf-8")
    if re.search(r"^MINIMAX_VOICE_ID=.*$", text, re.M):
        text = re.sub(r"^MINIMAX_VOICE_ID=.*$", f"MINIMAX_VOICE_ID={voice_id}", text, flags=re.M)
    else:
        text += f"\nMINIMAX_VOICE_ID={voice_id}\n"
    env.write_text(text, encoding="utf-8")
    print(f"  已写入 .env: MINIMAX_VOICE_ID={voice_id}")


def main() -> int:
    if not config.HAS_MINIMAX_KEY:
        print("  未配置 MINIMAX_API_KEY")
        return 1
    for p in (MAIN_AUDIO, EXAMPLE_AUDIO):
        if not p.exists():
            print(f"  缺少 {p}，请先跑 tools/prepare_v4c_reference.py")
            return 1

    voice_id = DEFAULT_VOICE_ID
    if "--voice-id" in sys.argv:
        voice_id = sys.argv[sys.argv.index("--voice-id") + 1]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{7,255}", voice_id):
        print(f"  voice_id 不合规（需以字母开头、长度 8~256、仅字母数字-_）：{voice_id}")
        return 1

    print("=" * 68)
    print("MiniMax 音色克隆（初音未来 V4C）")
    print("=" * 68)
    print(f"  key      : {redact(config.MINIMAX_API_KEY)}")
    print(f"  base_url : {config.MINIMAX_BASE_URL}")
    print(f"  主参考   : {MAIN_AUDIO.name}")
    print(f"  示例音频 : {EXAMPLE_AUDIO.name}  text={EXAMPLE_TEXT!r}")
    print()

    try:
        main_id = upload(MAIN_AUDIO, "voice_clone")
        prompt_id = upload(EXAMPLE_AUDIO, "prompt_audio")
        result = clone(main_id, prompt_id, voice_id)
    except Exception as exc:  # noqa: BLE001
        print(f"\n  ❌ {exc}")
        return 1

    demo = result.get("demo_audio") or ""
    info = result.get("extra_info") or {}
    print(f"\n  ✅ 克隆成功  voice_id = {voice_id}")
    if info:
        print(f"     试听时长 {info.get('audio_length', '?')} ms  "
              f"计费字数 {info.get('usage_characters', '?')}")

    if demo:
        dest = download(demo, OUT / f"minimax_clone_{voice_id}.mp3")
        if dest:
            print(f"  试听音频：{dest.relative_to(BASE)}")
            if "--play" in sys.argv:
                try:
                    import av
                    import numpy as np
                    import sounddevice as sd
                    import soundfile as sf

                    c = av.open(str(dest))
                    rs = av.AudioResampler(format="fltp", layout="mono", rate=44100)
                    chunks = []
                    for fr in c.decode(c.streams.audio[0]):
                        chunks += [o.to_ndarray().reshape(-1) for o in rs.resample(fr)]
                    chunks += [o.to_ndarray().reshape(-1) for o in rs.resample(None)]
                    c.close()
                    d = np.concatenate(chunks)
                    print("  ▶ 播放试听 …")
                    sd.play(d, 44100)
                    time.sleep(len(d) / 44100 + 0.4)
                    sd.stop()
                except Exception as exc:  # noqa: BLE001
                    print(f"  播放失败：{exc}")
    else:
        print("     （未返回试听音频）")

    update_env(voice_id)
    print("\n  ⚠️ 注意：克隆音色 168 小时内未使用会被系统删除；")
    print("     本项目的「启动预热」会自动用一次，所以只要常开桌宠就不会过期。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
