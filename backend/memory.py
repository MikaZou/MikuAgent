"""记忆系统：基于 SQLite 的会话、消息与长期记忆存储。"""
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
