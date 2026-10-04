"""记忆系统：基于 SQLite 的会话、消息与长期记忆存储。

**手机与 PC 各存一份同构的库**，连上之后双向同步（见 `shared/sync_rules.json`
与 docs/TECHNICAL.md §5.12）。为此三张内容表都多了四个同步列：

    uuid       —— 全局身份。两端各自 AUTOINCREMENT 的 id 没法当身份用
    updated_at —— epoch 秒，LWW 的新旧判定与同步游标
    deleted    —— 墓碑。删除必须能传播，否则两端会互相「复活」
    origin     —— 'pc' / 'phone'，只做溯源，不参与判定

会话**按天归并**（`substr(created_at,1,10)`），所以同步时不需要做
会话 id ↔ uuid 的映射 —— 两端本来就都是「一天一条」。
"""
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import sync_rules


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _now_epoch() -> float:
    return time.time()


def _new_uuid() -> str:
    """与手机端同形状：32 位十六进制、无短横线。"""
    return uuid.uuid4().hex


def _today() -> str:
    """今天的日期（本地时区，``YYYY-MM-DD``）。

    会话按「天」划分，所以这个值同时是**会话的分组键**和新建会话的标题。
    切分点是本地 00:00 —— 凌晨聊天算第二天。
    """
    return datetime.now().strftime("%Y-%m-%d")


class MemoryStore:
    """MikuAgent 的持久化记忆层。"""

    def __init__(self, db_path: Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    uuid TEXT,
                    title TEXT NOT NULL DEFAULT '新的对话',
                    created_at TEXT NOT NULL,
                    updated_at REAL NOT NULL DEFAULT 0,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    origin TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    uuid TEXT,
                    session_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    emotion TEXT,
                    created_at TEXT NOT NULL,
                    updated_at REAL NOT NULL DEFAULT 0,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    origin TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS memory_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    uuid TEXT,
                    content TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT '其他',
                    importance INTEGER NOT NULL DEFAULT 3,
                    source TEXT,
                    created_at TEXT NOT NULL,
                    updated_at REAL NOT NULL DEFAULT 0,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    origin TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL DEFAULT 0,
                    deleted INTEGER NOT NULL DEFAULT 0
                );
                """
            )
        self._migrate()

    def _migrate(self) -> None:
        """给本改动之前建的库补列 + 回填。幂等，每次启动跑一遍。

        ⚠️ 历史行的 `updated_at` 是用 `strftime('%s', created_at)` 回填的：
        SQLite 会把「本地时间字符串」当 **UTC** 解析，所以算出来的 epoch
        比真实时间早几个小时（东八区）。这对**追加型**的消息无害
        （只用于 LWW 与游标），但意味着首次同步必须**从 0 开始拉**，
        不能假设「只有比现在新的才要」。
        """
        columns = {
            "sessions": [
                ("uuid", "TEXT"),
                ("updated_at", "REAL NOT NULL DEFAULT 0"),
                ("deleted", "INTEGER NOT NULL DEFAULT 0"),
                ("origin", "TEXT NOT NULL DEFAULT ''"),
            ],
            "messages": [
                ("uuid", "TEXT"),
                ("updated_at", "REAL NOT NULL DEFAULT 0"),
                ("deleted", "INTEGER NOT NULL DEFAULT 0"),
                ("origin", "TEXT NOT NULL DEFAULT ''"),
            ],
            "memory_items": [
                ("uuid", "TEXT"),
                ("updated_at", "REAL NOT NULL DEFAULT 0"),
                ("deleted", "INTEGER NOT NULL DEFAULT 0"),
                ("origin", "TEXT NOT NULL DEFAULT ''"),
            ],
            "meta": [
                ("updated_at", "REAL NOT NULL DEFAULT 0"),
                ("deleted", "INTEGER NOT NULL DEFAULT 0"),
            ],
        }
        with self._connect() as conn:
            for table, extra in columns.items():
                have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                for column, decl in extra:
                    if column not in have:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

            # 回填（唯一索引必须在回填之后建，否则 NULL 会撞唯一约束）
            for table in ("sessions", "messages", "memory_items"):
                conn.execute(
                    f"UPDATE {table} SET uuid = lower(hex(randomblob(16))) "
                    "WHERE uuid IS NULL OR uuid = ''"
                )
                conn.execute(
                    f"UPDATE {table} SET updated_at = CAST(strftime('%s', created_at) AS REAL) "
                    "WHERE updated_at = 0"
                )
                conn.execute(
                    f"UPDATE {table} SET origin = 'pc' WHERE origin = ''"
                )
                conn.execute(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_uuid ON {table}(uuid)"
                )
            conn.execute("UPDATE meta SET updated_at = ? WHERE updated_at = 0", (_now_epoch(),))
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_updated ON messages(updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_updated ON memory_items(updated_at)")

    # ---------- 会话 ----------
    def create_session(self, title: Optional[str] = None) -> dict:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO sessions (uuid, title, created_at, updated_at, deleted, origin) "
                "VALUES (?, ?, ?, ?, 0, 'pc')",
                (_new_uuid(), title or "新的对话", _now(), _now_epoch()),
            )
            session_id = cur.lastrowid
        return self.get_session(session_id)

    def get_session(self, session_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def rename_session(self, session_id: int, title: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                (title.strip() or "新的对话", _now_epoch(), session_id),
            )

    def list_sessions(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.id, s.title, s.created_at,
                       COUNT(m.id) AS message_count,
                       MAX(m.created_at) AS last_active
                FROM sessions s
                LEFT JOIN messages m ON m.session_id = s.id AND m.deleted = 0
                WHERE s.deleted = 0
                GROUP BY s.id
                ORDER BY COALESCE(last_active, s.created_at) DESC, s.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_or_create_today(self) -> dict:
        """今天的会话；已存在就复用，不存在才新建（标题即当天日期）。

        **设计：每天一个会话，桌面端 / 手机端 / 网页端共享同一条。**
        所以在桌面聊完切到手机，能直接接上同一个话题 —— 而不是各开一条线。

        为什么靠 `created_at` 前缀判断而不是加 `day` 字段：那样要写数据迁移，
        而 `created_at` 本来就是本地时间字符串（`_now()`），前缀比较足够且零风险。
        代价是同一天可能残留多条历史会话（改动之前分裂出来的），
        这里取**最近活跃**那条继续，不合并也不删除用户数据。

        排序与 `list_sessions()` 保持一致，避免两处「最近」的定义不同。
        """
        today = _today()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT s.id, s.title, s.created_at,
                       COUNT(m.id) AS message_count,
                       MAX(m.created_at) AS last_active
                FROM sessions s
                LEFT JOIN messages m ON m.session_id = s.id AND m.deleted = 0
                WHERE substr(s.created_at, 1, 10) = ? AND s.deleted = 0
                GROUP BY s.id
                ORDER BY COALESCE(last_active, s.created_at) DESC, s.id DESC
                LIMIT 1
                """,
                (today,),
            ).fetchone()
        if row:
            return dict(row)
        return self.create_session(title=today)

    def resolve_session(self, preferred_id=None) -> dict:
        """返回今天的会话。**这是三端唯一该用的入口。**

        `preferred_id` 是**故意不参与选择**的，只为了保持调用方签名稳定
        （将来若真要做「一天多条会话」再在这里分支）。原因实测踩过：

        本改动落地前，同一天已经残留了多条会话。如果规则写成「只要是今天的
        id 就沿用」，那么手机端缓存着它那一条、桌面端用 `get_or_create_today()`
        取最近活跃的另一条，**两端会永久分裂**，跨设备接着聊就永远实现不了。
        所以规则收紧成：会话**只由日期决定**，客户端说不上话。

        效果：无论谁先开口，消息都落进同一条；另一端的下一条也会被带过来，
        从此收敛到同一条线上。
        """
        return self.get_or_create_today()

    def delete_session(self, session_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    # ---------- 消息 ----------
    def add_message(
        self,
        session_id: int,
        role: str,
        content: str,
        emotion: Optional[str] = None,
    ) -> dict:
        ts = _now_epoch()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO messages "
                "(uuid, session_id, role, content, emotion, created_at, updated_at, deleted, origin) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'pc')",
                (_new_uuid(), session_id, role, content, emotion, _now(), ts),
            )
            message_id = cur.lastrowid
            # 会话自身的 updated_at 也要跟上，否则同步「按变动取天」会漏掉这一天
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ? AND updated_at < ?",
                (ts, session_id, ts),
            )
        return self.get_message(message_id)

    def get_message(self, message_id: int) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        return dict(row)

    def get_messages(self, session_id: int, limit: Optional[int] = None) -> list[dict]:
        if limit:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM (
                        SELECT * FROM messages
                        WHERE session_id = ? AND deleted = 0
                        ORDER BY id DESC LIMIT ?
                    ) ORDER BY id
                    """,
                    (session_id, limit),
                ).fetchall()
        else:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE session_id = ? AND deleted = 0 ORDER BY id",
                    (session_id,),
                ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 长期记忆 ----------
    def add_memory(
        self,
        content: str,
        category: str = "其他",
        importance: int = 3,
        source: str = "对话",
    ) -> dict:
        content = content.strip()
        if not content:
            raise ValueError("记忆内容不能为空")
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO memory_items "
                "(uuid, content, category, importance, source, created_at, updated_at, deleted, origin) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'pc')",
                (_new_uuid(), content, category.strip() or "其他",
                 max(1, min(5, importance)), source, _now(), _now_epoch()),
            )
            memory_id = cur.lastrowid
        return self.get_memory(memory_id)

    def get_memory(self, memory_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM memory_items WHERE id = ?", (memory_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_memories(self, limit: int = 200) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_items WHERE deleted = 0 "
                "ORDER BY importance DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_memory(self, memory_id: int) -> bool:
        """**打墓碑**而不是物理删除：删除必须能同步到另一端，
        否则下次同步时对方那条会把本地的删除「复活」。
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE memory_items SET deleted = 1, updated_at = ? WHERE id = ?",
                (_now_epoch(), memory_id),
            )
        return cur.rowcount > 0

    # ---------- 元数据（用户昵称等） ----------
    def get_meta(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO meta (key, value, updated_at, deleted) VALUES (?, ?, ?, 0) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at, deleted = 0",
                (key, value, _now_epoch()),
            )

    # ================================================================ 双端同步
    #
    # 手机是发起方（它知道自己什么时候能连上），PC 只负责「给变动」与「收变动」，
    # 因此 **PC 侧不需要游标状态**：拉取游标与推送游标都存在手机那边。

    def sessions_of_day(self, day: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE substr(created_at,1,10) = ? AND deleted = 0",
                (day,),
            ).fetchall()
        return [dict(r) for r in rows]

    def session_message_count(self, session_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE session_id = ? AND deleted = 0",
                (session_id,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def primary_session_of_day(self, day: str) -> Optional[dict]:
        """该天「消息最多」的那条会话；同一天有多条碎片时靠它收敛。

        选择规则在 `sync_rules.pick_primary`（与手机端同一份契约），
        必须完全确定，否则两端各挑一条，消息就永远分在两处。
        """
        rows = self.sessions_of_day(day)
        if not rows:
            return None
        candidates = [
            {
                "id": r["id"],
                "created_at": r["created_at"],
                "message_count": self.session_message_count(r["id"]),
            }
            for r in rows
        ]
        best = sync_rules.pick_primary(candidates)
        return self.get_session(best) if best is not None else None

    def changes_since(self, since: float, limit: Optional[int] = None) -> dict:
        """取出 `updated_at > since` 的变动，**按天打包**。

        打包单位是「天」而不是会话：两端都是「一天一条会话」，
        所以接收端按日期找自己那一条就行，不需要知道发送端的会话 id。
        """
        capped = int(limit or sync_rules.plan_limit())
        with self._connect() as conn:
            messages = [
                dict(r) for r in conn.execute(
                    "SELECT * FROM messages WHERE updated_at > ? "
                    "ORDER BY updated_at LIMIT ?",
                    (since, capped),
                ).fetchall()
            ]
            memories = [
                dict(r) for r in conn.execute(
                    "SELECT * FROM memory_items WHERE updated_at > ? "
                    "ORDER BY updated_at LIMIT ?",
                    (since, capped),
                ).fetchall()
            ]
            meta_rows = [
                dict(r) for r in conn.execute(
                    "SELECT * FROM meta WHERE updated_at > ?", (since,)
                ).fetchall()
            ]

        days: dict[str, list[dict]] = {}
        for m in messages:
            days.setdefault(sync_rules.day_of(m["created_at"]), []).append(m)

        out_days = []
        for day, msgs in days.items():
            if not day:
                continue
            session = self.primary_session_of_day(day)
            out_days.append({
                "day": day,
                "title": (session or {}).get("title") or day,
                "updated_at": max(m["updated_at"] for m in msgs),
                "deleted": 0,
                "messages": [self._wire_message(m) for m in msgs],
            })

        return {
            "days": out_days,
            "memories": [self._wire_memory(m) for m in memories],
            "meta": {
                r["key"]: {
                    "value": r["value"],
                    "updated_at": r["updated_at"],
                    "deleted": int(r["deleted"]),
                }
                for r in meta_rows
            },
        }

    def apply_bundle(self, bundle: dict) -> dict:
        """把手机送来的一批变动合并进来。整体一个事务。

        冲突判定一律走 `sync_rules.decide`（契约在 `shared/sync_rules.json`）。
        """
        inserted = updated = kept = days_created = 0
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN")
            for day in bundle.get("days") or []:
                name = sync_rules.day_of(str(day.get("day") or ""))
                if not name:
                    continue
                session = self.primary_session_of_day(name)
                if session is None:
                    cur = conn.execute(
                        "INSERT INTO sessions (uuid, title, created_at, updated_at, deleted, origin) "
                        "VALUES (?, ?, ?, ?, 0, 'phone')",
                        (_new_uuid(), day.get("title") or name, f"{name} 00:00:00",
                         float(day.get("updated_at") or 0)),
                    )
                    session = {"id": cur.lastrowid, "updated_at": 0.0}
                    days_created += 1

                for msg in day.get("messages") or []:
                    local = conn.execute(
                        "SELECT * FROM messages WHERE uuid = ?", (msg["uuid"],)
                    ).fetchone()
                    verdict = sync_rules.decide(
                        None if local is None else float(local["updated_at"]),
                        float(msg["updated_at"]),
                    )
                    if verdict == sync_rules.INSERT:
                        conn.execute(
                            "INSERT INTO messages (uuid, session_id, role, content, emotion, "
                            "created_at, updated_at, deleted, origin) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (msg["uuid"], session["id"], msg["role"], msg["content"],
                             msg.get("emotion"), msg["created_at"], float(msg["updated_at"]),
                             int(msg.get("deleted") or 0), msg.get("origin") or "phone"),
                        )
                        inserted += 1
                    elif verdict == sync_rules.UPDATE:
                        conn.execute(
                            "UPDATE messages SET session_id = ?, role = ?, content = ?, emotion = ?, "
                            "created_at = ?, updated_at = ?, deleted = ? WHERE uuid = ?",
                            (session["id"], msg["role"], msg["content"], msg.get("emotion"),
                             msg["created_at"], float(msg["updated_at"]),
                             int(msg.get("deleted") or 0), msg["uuid"]),
                        )
                        updated += 1
                    else:
                        kept += 1

                # 会话自身的标题/时间戳也按 LWW 走一次
                if sync_rules.decide(
                    float(session.get("updated_at") or 0), float(day.get("updated_at") or 0)
                ) == sync_rules.UPDATE:
                    conn.execute(
                        "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                        (day.get("title") or name, float(day.get("updated_at") or 0),
                         session["id"]),
                    )

            for mem in bundle.get("memories") or []:
                local = conn.execute(
                    "SELECT * FROM memory_items WHERE uuid = ?", (mem["uuid"],)
                ).fetchone()
                verdict = sync_rules.decide(
                    None if local is None else float(local["updated_at"]),
                    float(mem["updated_at"]),
                )
                if verdict == sync_rules.INSERT:
                    conn.execute(
                        "INSERT INTO memory_items (uuid, content, category, importance, source, "
                        "created_at, updated_at, deleted, origin) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (mem["uuid"], mem["content"], mem.get("category") or "其他",
                         int(mem.get("importance") or 3), mem.get("source"),
                         mem["created_at"], float(mem["updated_at"]),
                         int(mem.get("deleted") or 0), mem.get("origin") or "phone"),
                    )
                    inserted += 1
                elif verdict == sync_rules.UPDATE:
                    conn.execute(
                        "UPDATE memory_items SET content = ?, category = ?, importance = ?, "
                        "source = ?, updated_at = ?, deleted = ? WHERE uuid = ?",
                        (mem["content"], mem.get("category") or "其他",
                         int(mem.get("importance") or 3), mem.get("source"),
                         float(mem["updated_at"]), int(mem.get("deleted") or 0), mem["uuid"]),
                    )
                    updated += 1
                else:
                    kept += 1

            for key, entry in (bundle.get("meta") or {}).items():
                local = conn.execute(
                    "SELECT updated_at FROM meta WHERE key = ?", (key,)
                ).fetchone()
                verdict = sync_rules.decide(
                    None if local is None else float(local["updated_at"]),
                    float(entry["updated_at"]),
                )
                if verdict in (sync_rules.INSERT, sync_rules.UPDATE):
                    conn.execute(
                        "INSERT INTO meta (key, value, updated_at, deleted) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                        "updated_at = excluded.updated_at, deleted = excluded.deleted",
                        (key, entry.get("value") or "", float(entry["updated_at"]),
                         int(entry.get("deleted") or 0)),
                    )
                    inserted += 1 if local is None else 0
                    updated += 0 if local is None else 1
                else:
                    kept += 1

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        return {
            "inserted": inserted, "updated": updated, "kept": kept,
            "days_created": days_created,
        }

    # 对外只暴露同步需要的字段，顺带把 sqlite 的 0/1 转成 JSON 布尔形状的数字
    @staticmethod
    def _wire_message(m: dict) -> dict:
        return {
            "uuid": m["uuid"], "role": m["role"], "content": m["content"],
            "emotion": m.get("emotion"), "created_at": m["created_at"],
            "updated_at": float(m["updated_at"]), "deleted": int(m["deleted"]),
            "origin": m.get("origin") or "pc",
        }

    @staticmethod
    def _wire_memory(m: dict) -> dict:
        return {
            "uuid": m["uuid"], "content": m["content"], "category": m["category"],
            "importance": int(m["importance"]), "source": m.get("source"),
            "created_at": m["created_at"], "updated_at": float(m["updated_at"]),
            "deleted": int(m["deleted"]), "origin": m.get("origin") or "pc",
        }

    def counts(self) -> dict:
        with self._connect() as conn:
            out = {}
            for name, table in (("sessions", "sessions"), ("messages", "messages"),
                                ("memories", "memory_items")):
                row = conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE deleted = 0"
                ).fetchone()
                out[name] = int(row["n"])
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE deleted = 1"
            ).fetchone()
            out["tombstones"] = int(row["n"])
        return out
