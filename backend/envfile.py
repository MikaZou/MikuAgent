""".env 的就地读写工具。

设置向导要把用户的选择写回 .env，但 .env 里有大量注释和分组，
**不能整文件重写**（会把注释全丢掉）。这里只做「按 key 替换值，
不存在则追加」，其余内容原样保留。

也用于读取当前值（向导打开时要回显用户上次的选择）。
"""
from __future__ import annotations

import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"


def read_env(path: Path | None = None) -> dict[str, str]:
    """解析 .env，返回 {key: value}（忽略注释与空行）。"""
    p = path or ENV_PATH
    out: dict[str, str] = {}
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        val = v.strip()
        # 写回时对含 # 的值加过引号，读回要还原，保证往返一致
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[k.strip()] = val
    return out


def update_env(values: dict[str, str], path: Path | None = None) -> list[str]:
    """把 values 写回 .env：已有的 key 就地替换，没有的追加到末尾。

    返回实际发生变化的 key 列表。值里若含 ``#`` 会被引号包起来，
    避免被当成行内注释截断。
    """
    p = path or ENV_PATH
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    lines = text.splitlines()
    changed: list[str] = []

    for key, raw in values.items():
        val = str(raw)
        # 含 # 或首尾空格时加引号，防止被解析成注释
        if "#" in val or val != val.strip():
            val = f'"{val}"'
        line_new = f"{key}={val}"
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
        hit = False
        for i, line in enumerate(lines):
            if pattern.match(line):
                if lines[i] != line_new:
                    lines[i] = line_new
                    changed.append(key)
                hit = True
                break
        if not hit:
            lines.append(line_new)
            changed.append(key)

    if changed:
        p.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    return changed


def describe_tts_engine(engine: str) -> str:
    return {
        "minimax": "云端 MiniMax（克隆初音音色，最省资源，需联网计费）",
        "sovits": "本地 GPT-SoVITS（音色最准，约 1.4~2.2GB 显存 + 2.9GB 内存）",
        "edge": "微软在线 TTS（轻量，无初音音色）",
        "none": "关闭语音输出",
    }.get(engine, engine)


def describe_stt_transcriber(name: str) -> str:
    return {
        "local-whisper": "本地 Whisper（约 1GB 内存，离线可用）",
        "minimax": "云端 MiniMax ASR（几乎不吃内存，需联网）",
    }.get(name, name)
