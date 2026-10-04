"""回归：PC 侧的 /sync/* 端点与合并行为。

只测 PC 半边（手机那半边由真机 e2e 覆盖），但这一半必须自己站得住：

* 拉的形状对不对（按天打包、字段齐全）
* 收得下、并且**幂等**（同一批重复送，行数不变 —— 这是「游标回退 300 秒重扫」
  能成立的前提）
* LWW 生效（更新的赢、更旧的输）
* 墓碑能传播（删除不在下次同步时被对方复活）

用临时库，绝不碰用户的 data/mikuagent.db。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "backend"))

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from memory import MemoryStore  # noqa: E402
from remote_server import RemoteServer  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILED.append(name)


def phone_message(uuid: str, content: str, ts: float, *, deleted: int = 0, role: str = "user") -> dict:
    return {
        "uuid": uuid, "role": role, "content": content, "emotion": None,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"), "updated_at": ts,
        "deleted": deleted, "origin": "phone",
    }


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="miku-sync-"))
    db = tmp / "mikuagent.db"
    store = MemoryStore(db)

    # 先在 PC 上造一点历史：昨天一条、今天一条
    today = time.strftime("%Y-%m-%d")
    session = store.get_or_create_today()
    store.add_message(session["id"], "user", "PC 今天说的话")
    store.add_message(session["id"], "assistant", "PC 今天的回答", emotion="HAPPY")
    store.add_memory("主人喜欢葱", "偏好", 4, "对话")
    store.set_meta("user_name", "主人")

    server = RemoteServer(agent=None, tts=None, stt=None, memory=store)
    app = server._make_app()

    try:
        async with TestClient(TestServer(app)) as client:
            # ---- 1) 探活 ----
            resp = await client.get("/sync/state")
            state = await resp.json()
            check("GET /sync/state 可用",
                  resp.status == 200 and "counts" in state and "now" in state,
                  json.dumps(state.get("counts"), ensure_ascii=False))
            check("PC 侧的行都带了 uuid（迁移生效）",
                  state["counts"]["messages"] >= 2 and state["counts"]["memories"] >= 1,
                  json.dumps(state["counts"], ensure_ascii=False))

            # ---- 2) 全量拉 ----
            resp = await client.get("/sync/changes?since=0")
            full = await resp.json()
            check("GET /sync/changes 返回 now 与 has_more",
                  "now" in full and "has_more" in full, json.dumps({k: full[k] for k in ("now", "has_more")}))
            days = {d["day"]: d for d in full["days"]}
            check("按天打包", today in days, str(sorted(days)))
            msgs = days[today]["messages"]
            check("今天的消息都在", len(msgs) >= 2, f"{len(msgs)} 条")
            check("消息字段齐全（手机端要靠它建行）",
                  all(set(m) >= {"uuid", "role", "content", "emotion", "created_at",
                                 "updated_at", "deleted", "origin"} for m in msgs),
                  str(sorted(msgs[0])))
            check("长期记忆也在", len(full["memories"]) >= 1, str(len(full["memories"])))
            check("meta（昵称）也在", full["meta"].get("user_name", {}).get("value") == "主人",
                  json.dumps(full["meta"], ensure_ascii=False))

            # ---- 3) 手机推上来一批 ----
            now = time.time()
            push = {
                "days": [{
                    "day": today, "title": today, "updated_at": now, "deleted": 0,
                    "messages": [
                        phone_message("phone-1", "手机离线时说的话", now),
                        phone_message("phone-2", "手机离线时的回答", now + 0.01,
                                      role="assistant"),
                    ],
                }],
                "memories": [{
                    "uuid": "phone-mem-1", "content": "小邹最喜欢吃葱", "category": "偏好",
                    "importance": 5, "source": "对话",
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "updated_at": now, "deleted": 0, "origin": "phone",
                }],
                "meta": {"user_name": {"value": "小邹", "updated_at": now, "deleted": 0}},
            }
            resp = await client.post("/sync/changes", json=push)
            applied = (await resp.json())["applied"]
            check("POST /sync/changes 合并成功",
                  resp.status == 200 and applied["inserted"] >= 3, json.dumps(applied, ensure_ascii=False))
            check("手机的消息并进了**当天同一条**会话（不是新建一条）",
                  applied["days_created"] == 0, f"days_created={applied['days_created']}")

            # 合并后：PC 上应该能看到手机那两条，而且仍在同一条会话里
            merged = store.get_messages(session["id"])
            contents = [m["content"] for m in merged]
            check("PC 能读到手机发来的消息",
                  "手机离线时说的话" in contents and "手机离线时的回答" in contents,
                  str(contents))
            check("PC 的昵称被手机的 LWW 覆盖",
                  store.get_meta("user_name") == "小邹", store.get_meta("user_name"))

            # ---- 4) 幂等：同一批再送一次 ----
            before = store.counts()
            resp = await client.post("/sync/changes", json=push)
            again = (await resp.json())["applied"]
            after = store.counts()
            check("重复推送零插入", again["inserted"] == 0, json.dumps(again))
            check("重复推送后行数不变（幂等）", before == after, f"{before} → {after}")

            # ---- 5) LWW：更新的赢、更旧的输 ----
            newer = {"days": [{"day": today, "title": today, "updated_at": now + 100, "deleted": 0,
                               "messages": [phone_message("phone-1", "改过的内容", now + 100)]}],
                     "memories": [], "meta": {}}
            await client.post("/sync/changes", json=newer)
            got = [m["content"] for m in store.get_messages(session["id"]) if m["uuid"] == "phone-1"]
            check("更新的内容覆盖了旧的", got == ["改过的内容"], str(got))

            older = {"days": [{"day": today, "title": today, "updated_at": now - 100, "deleted": 0,
                               "messages": [phone_message("phone-1", "更旧的内容", now - 100)]}],
                     "memories": [], "meta": {}}
            await client.post("/sync/changes", json=older)
            got = [m["content"] for m in store.get_messages(session["id"]) if m["uuid"] == "phone-1"]
            check("更旧的内容不会覆盖新的", got == ["改过的内容"], str(got))

            # ---- 6) 墓碑 ----
            tombstone = {"days": [{"day": today, "title": today, "updated_at": now + 200, "deleted": 0,
                                   "messages": [phone_message("phone-2", "手机离线时的回答",
                                                              now + 200, deleted=1)]}],
                         "memories": [], "meta": {}}
            await client.post("/sync/changes", json=tombstone)
            visible = [m["uuid"] for m in store.get_messages(session["id"])]
            check("墓碑生效：已删的不再出现", "phone-2" not in visible, str(visible))

            # 拉回来的增量里必须**带上**这条墓碑，否则手机永远删不掉
            delta = await (await client.get(f"/sync/changes?since={now + 50}")).json()
            all_msgs = [m for d in delta["days"] for m in d["messages"]]
            check("增量里带着墓碑（否则删除会被对方复活）",
                  any(m["uuid"] == "phone-2" and m["deleted"] == 1 for m in all_msgs),
                  str([(m["uuid"], m["deleted"]) for m in all_msgs]))

            # ---- 7) 增量游标 ----
            # 注意游标要取到**比上面故意推的未来时间戳还新** ——
            # 那些 now+100/now+200 是在模拟「手机时钟偏快」，
            # 用 time.time()+10 会被它们盖住（第一版测试就写错了）。
            empty = await (await client.get(f"/sync/changes?since={now + 1000}")).json()
            check("超过所有变动的时间点拉到空（游标生效）",
                  not empty["days"] and not empty["memories"], json.dumps(empty)[:120])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
