"""记忆系统：基于 SQLite 的会话、消息与长期记忆存储。"""
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
                    title TEXT NOT NULL DEFAULT '新的对话',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    emotion TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS memory_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT '其他',
                    importance INTEGER NOT NULL DEFAULT 3,
                    source TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    # ---------- 会话 ----------
    def create_session(self, title: Optional[str] = None) -> dict:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO sessions (title, created_at) VALUES (?, ?)",
                (title or "新的对话", _now()),
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
                "UPDATE sessions SET title = ? WHERE id = ?",
                (title.strip() or "新的对话", session_id),
            )

    def list_sessions(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.id, s.title, s.created_at,
                       COUNT(m.id) AS message_count,
                       MAX(m.created_at) AS last_active
                FROM sessions s
                LEFT JOIN messages m ON m.session_id = s.id
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
                LEFT JOIN messages m ON m.session_id = s.id
                WHERE substr(s.created_at, 1, 10) = ?
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
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO messages (session_id, role, content, emotion, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, role, content, emotion, _now()),
            )
            message_id = cur.lastrowid
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
                        SELECT * FROM messages WHERE session_id = ?
                        ORDER BY id DESC LIMIT ?
                    ) ORDER BY id
                    """,
                    (session_id, limit),
                ).fetchall()
        else:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE session_id = ? ORDER BY id",
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
                "INSERT INTO memory_items (content, category, importance, source, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (content, category.strip() or "其他", max(1, min(5, importance)), source, _now()),
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
                "SELECT * FROM memory_items ORDER BY importance DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_memory(self, memory_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM memory_items WHERE id = ?", (memory_id,))
        return cur.rowcount > 0

    # ---------- 元数据（用户昵称等） ----------
    def get_meta(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
