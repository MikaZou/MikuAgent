"""只读看一眼记忆库的现状（排查 / 对比迁移前后用）。

用法：
    .venv\\Scripts\\python.exe tools/inspect_memory.py                    # 默认库
    .venv\\Scripts\\python.exe tools/inspect_memory.py <别的.db>
    .venv\\Scripts\\python.exe tools/inspect_memory.py --emotions         # 顺便看助手消息的情感标签

`--emotions` 是排查「标签解析」用的：助手消息的 `emotion` 与正文开头对不对得上。
真机上踩过一次 —— 模型把标签写成不带方括号的 `HAPPY 收到收到～…`，
正文里就带着 "HAPPY" 显示出来、TTS 还会照着念（见 model 里的 BARE_EMOTION_TAG）。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

args = [a for a in sys.argv[1:] if not a.startswith("--")]
show_emotions = "--emotions" in sys.argv
db = Path(args[0]) if args else BASE / "data" / "mikuagent.db"

print(f"库：{db}  大小 {db.stat().st_size if db.exists() else '-'}")
if not db.exists():
    raise SystemExit(0)

conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
tables = [r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
print("表：", tables)

for t in ("sessions", "messages", "memory_items", "meta"):
    if t in tables:
        print(f"  {t}: {conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}")

if "messages" in tables:
    print("messages 列：", [r[1] for r in conn.execute("PRAGMA table_info(messages)")])

if "meta" in tables:
    print("meta：", [(r["key"], r["value"]) for r in conn.execute("SELECT * FROM meta")])

if "memory_items" in tables:
    print("记忆：", [r["content"] for r in conn.execute(
        "SELECT content FROM memory_items ORDER BY id DESC LIMIT 6")])

if "messages" in tables:
    print("最近消息：", [r["content"][:26] for r in conn.execute(
        "SELECT content FROM messages ORDER BY id DESC LIMIT 5")])
    if show_emotions:
        print("最近助手消息（emotion / 正文）：")
        for r in reversed(conn.execute(
                "SELECT emotion, content FROM messages WHERE role='assistant' "
                "ORDER BY id DESC LIMIT 8").fetchall()):
            print(f"    [{r['emotion']}] {r['content'][:60]}")
