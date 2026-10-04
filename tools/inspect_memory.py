"""只读看一眼真实记忆库的现状（同步改动前/后的对照用）。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
db = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE / "data" / "mikuagent.db"

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
