"""端到端验收：手机端与 PC 的双端记忆同步。

要跑的东西（对应 docs/TECHNICAL.md §5.12 的验收表）：

  4  手机离线聊过几轮 → 连上 PC 后，那几轮出现在 PC 的库里
  5  PC 先聊过 → 手机连上后能看到并能接着聊
  6  两端同一天都聊过 → 同步后**当天只有一条会话**，消息合并、无重复
  7  长期记忆双向可见
  8  重复同步幂等（行数与内容都不变）

手机侧走**真实的同步代码路径**：用 `native.syncNow()` 触发，
用 `native.deviceState()` 读结果；PC 侧直接读 `data/mikuagent.db`。

前置：PC 端 MikuAgent 已启动、`adb reverse tcp:8765 tcp:8765` 已建、手机 App 在跑。

用法：
    .venv\\Scripts\\python.exe tools/test_sync_e2e.py
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "tools"))

import phone_cdp  # noqa: E402

DB = BASE_DIR / "data" / "mikuagent.db"

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILED.append(name)


# ------------------------------------------------------------------ 两端读状态

def pc_state() -> dict:
    import sqlite3

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    out = {
        "counts": {
            t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE deleted = 0").fetchone()[0]
            for t in ("sessions", "messages", "memory_items")
        },
        "memories": [r["content"] for r in conn.execute(
            "SELECT content FROM memory_items WHERE deleted = 0 ORDER BY id")],
        "by_day": {},
        "phone_origin": conn.execute(
            "SELECT COUNT(*) FROM messages WHERE origin = 'phone' AND deleted = 0"
        ).fetchone()[0],
    }
    for row in conn.execute(
        "SELECT substr(created_at,1,10) AS day, "
        "COUNT(DISTINCT session_id) AS sessions, COUNT(*) AS messages "
        "FROM messages WHERE deleted = 0 GROUP BY day"
    ):
        out["by_day"][row["day"]] = {"sessions": row["sessions"], "messages": row["messages"]}
    # 会话表层面：同一天到底有几条会话（碎片会让这个数字 >1）
    out["sessions_per_day"] = {
        r["day"]: r["n"] for r in conn.execute(
            "SELECT substr(created_at,1,10) AS day, COUNT(*) AS n FROM sessions "
            "WHERE deleted = 0 GROUP BY day"
        )
    }
    conn.close()
    return out


def phone_eval(expression: str) -> str:
    """同步地在真机页面上求值（复用 tools/phone_cdp.py 的实现）。"""
    return subprocess.run(
        [sys.executable, str(BASE_DIR / "tools" / "phone_cdp.py"), expression],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout.strip().splitlines()[-1]


def phone_state() -> dict:
    raw = phone_eval("JSON.stringify(JSON.parse(native.deviceState()))")
    return json.loads(raw)


def phone_sync_now(timeout_s: float = 40.0) -> dict:
    """点一次「立即同步」，等它跑完，返回同步状态。

    没有「同步完成」事件，所以轮询 `sync.lastSyncAt` 变化 ——
    比固定 sleep 稳，也快。
    """
    import time

    before = phone_state()["sync"]["lastSyncAt"]
    phone_eval("native.syncNow()")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(1.5)
        st = phone_state()["sync"]
        if st["lastSyncAt"] != before:
            return st
    raise SystemExit("等同步完成超时（lastSyncAt 一直是 %.0f）" % before)


def main() -> int:
    if not DB.exists():
        print(f"PC 的库不存在：{DB}（先启动一次 PC 端）")
        return 2

    pc = pc_state()
    print(f"PC：{pc['counts']}  其中来自手机 {pc['phone_origin']} 条")

    # 手机端必须先跑起来且配了 PC 地址
    dev = phone_state()
    if not dev.get("host"):
        print("手机没配 PC 地址，先连一次再跑")
        return 2
    print(f"手机：{dev['memory']}  brain={dev['brainKind']}")

    # ---- 8) 幂等：先测这个，免得后面被自己污染 ----
    before_pc = pc_state()["counts"]
    before_phone = phone_state()["memory"]
    st1 = phone_sync_now()
    mid_pc = pc_state()["counts"]
    mid_phone = phone_state()["memory"]
    check("同步不会平白增加行数（第一次）",
          mid_pc == before_pc and mid_phone == before_phone,
          f"PC {before_pc} → {mid_pc} / 手机 {before_phone} → {mid_phone}")

    st2 = phone_sync_now()
    after_pc = pc_state()["counts"]
    after_phone = phone_state()["memory"]
    check("重复同步幂等（第二次行数也不变）",
          after_pc == mid_pc and after_phone == mid_phone,
          f"PC {mid_pc} → {after_pc} / 手机 {mid_phone} → {after_phone}")
    check("同步没报错", not st2.get("lastError"), st2.get("lastError") or "(无)")
    print(f"     最近一次：{st2.get('text')} / 拉 {st2.get('lastPullCount')} 推 {st2.get('lastPushCount')}")

    # ---- 4) 手机离线期间的消息到了 PC ----
    check("PC 库里有手机来源的消息（手机离线聊的内容已同步）",
          pc["phone_origin"] > 0, f"{pc['phone_origin']} 条")

    # ---- 5) PC 的历史到了手机 ----
    check("手机拿到了 PC 的历史（手机消息数 >= PC 消息数的一部分）",
          before_phone["messages"] >= 100 and before_phone["sessions"] >= 2,
          f"手机 {before_phone}")

    # ---- 6) 按天归并 ----
    # 用户库里 2026-09-11 / 13 / 14 各有好几条会话 —— 那是**本改动之前就存在的碎片**
    # （`resolve_session` 的注释里记着这事）。方案明确「不合并也不删除用户数据」，
    # 所以这里要保证的是「同步没有让它变多」以及「有手机消息的那天只落一处」。
    after_state = pc_state()
    check("同步没有让任何一天多出会话（不加重历史碎片）",
          after_state["sessions_per_day"] == pc["sessions_per_day"],
          f"{pc['sessions_per_day']} → {after_state['sessions_per_day']}")
    fragmented = {d: n for d, n in pc["sessions_per_day"].items() if n > 1}
    if fragmented:
        print(f"     （历史碎片保留原样，按设计不动：{fragmented}）")

    today = max(pc["by_day"].keys()) if pc["by_day"] else ""
    check(f"今天（{today}）只有一条会话（手机的消息并进去了，没有另开一条）",
          pc["sessions_per_day"].get(today) == 1,
          f"今天有 {pc['sessions_per_day'].get(today)} 条会话")
    check("手机端每天也只有一条会话（有消息的天数 = 手机的会话数）",
          before_phone["sessions"] == len(pc["by_day"]),
          f"手机 {before_phone['sessions']} 会话 / PC 有消息的天数 {len(pc['by_day'])}")
    check("PC 消息里没有重复 uuid 造成的翻倍",
          mid_pc["messages"] == pc["counts"]["messages"],
          f"{pc['counts']['messages']} → {mid_pc['messages']}")

    # 两端的**今天**应该指向同一条会话、条数一致
    day = max(pc["by_day"].keys()) if pc["by_day"] else ""
    if day:
        pc_today = pc["by_day"][day]["messages"]
        print(f"     {day}：PC {pc_today} 条 / 会话数 {pc['sessions_per_day'].get(day)}")
        check(f"PC 今天({day})有消息", pc_today > 0, str(pc_today))

    # ---- 7) 长期记忆双向 ----
    check("PC 上能看到手机写入的长期记忆",
          any("葱" in m for m in pc["memories"]),
          str(pc["memories"])[:120])
    check("手机也拿到了 PC 的长期记忆（记忆条数 >= PC 的条数）",
          before_phone["memories"] >= len(pc["memories"]),
          f"手机 {before_phone['memories']} / PC {len(pc['memories'])}")

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
