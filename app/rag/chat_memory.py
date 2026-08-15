"""会话记忆存储：基于 SQLite 的对话历史与摘要持久化。

设计：
- sessions 表：会话元信息（session_id / user_id / title / created_at / updated_at）。
- messages 表：每条对话消息（user/assistant），按 session_id + turn_index + role 索引，带 user_id。
  metadata 列存储 JSON 元数据：sources、tool_data、pdf 状态等。
- summaries 表：每个 session 唯一一条摘要，记录 up_to_turn。
- 线程安全：每次操作用独立连接 + 事务；FastAPI 多线程下安全。
- 不依赖向量检索；search() 改用 LIKE 关键词模糊匹配。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from functools import lru_cache
from typing import Any

from app.config import settings


@lru_cache(maxsize=1)
def get_chat_store() -> "ChatMemoryStore":
    """返回会话记忆存储单例。"""
    return ChatMemoryStore(str(settings.chat_db))


class ChatMemoryStore:
    """对话历史存储（SQLite 实现）。

    线程安全：内部用 threading.Lock 保护写操作；读操作用独立连接。
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        """返回新连接（启用 WAL，提升并发读性能）。"""
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        return conn

    def _init_db(self) -> None:
        """初始化表与索引；兼容旧表（自动 ALTER 补 user_id / metadata 列）。"""
        with self._lock:
            conn = self._conn()
            try:
                # 1) 建表（不建依赖 user_id 的索引，避免旧表 ALTER 前冲突）
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id  TEXT PRIMARY KEY,
                        user_id     TEXT NOT NULL,
                        title       TEXT NOT NULL,
                        created_at  INTEGER NOT NULL,
                        updated_at  INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_sessions_user
                        ON sessions(user_id, updated_at DESC);

                    CREATE TABLE IF NOT EXISTS messages (
                        id          TEXT PRIMARY KEY,
                        session_id  TEXT NOT NULL,
                        role        TEXT NOT NULL,
                        content     TEXT NOT NULL,
                        turn_index  INTEGER NOT NULL,
                        ts          INTEGER NOT NULL,
                        metadata    TEXT NOT NULL DEFAULT '{}'
                    );
                    CREATE INDEX IF NOT EXISTS idx_messages_session
                        ON messages(session_id, turn_index, role);

                    CREATE TABLE IF NOT EXISTS summaries (
                        session_id  TEXT PRIMARY KEY,
                        summary     TEXT NOT NULL,
                        up_to_turn  INTEGER NOT NULL,
                        ts          INTEGER NOT NULL
                    );
                    """
                )
                # 2) 兼容旧表：补 user_id 列
                cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
                if "user_id" not in cols:
                    conn.execute("ALTER TABLE messages ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
                if "metadata" not in cols:
                    conn.execute("ALTER TABLE messages ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'")
                # 3) 建依赖 user_id 的索引（确保列已存在）
                conn.executescript(
                    "CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, session_id);"
                )
                conn.commit()
            finally:
                conn.close()

    # —— 写入 ——

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        turn_index: int,
        user_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """追加一条消息。role: 'user' | 'assistant'。
        metadata 用于存储附加信息：sources、tool_data、pdf 状态等。
        """
        if role not in ("user", "assistant"):
            raise ValueError(f"role 必须是 user/assistant，收到 {role!r}")
        msg_id = f"{session_id}:{turn_index}:{role}"
        now = int(time.time())
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO messages(id, session_id, user_id, role, content, turn_index, ts, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (msg_id, session_id, user_id, role, content, int(turn_index), now, meta_json),
                )
                conn.commit()
            finally:
                conn.close()

    def add_turn(
        self,
        session_id: str,
        turn_index: int,
        user_msg: str,
        assistant_msg: str,
        user_id: str = "",
        title: str | None = None,
        assistant_metadata: dict[str, Any] | None = None,
    ) -> None:
        """追加一整轮对话（user + assistant）。

        首轮（turn_index=1）自动在 sessions 表写入会话元信息；后续轮次更新 updated_at。
        title 不传则用首轮 user_msg 前 30 字截断。
        assistant_metadata 会存入 assistant 消息的 metadata 列，用于持久化 sources / 工具数据等。
        """
        self.add_message(session_id, "user", user_msg, turn_index, user_id)
        self.add_message(session_id, "assistant", assistant_msg, turn_index, user_id, metadata=assistant_metadata)
        # 维护 sessions 表
        now = int(time.time())
        if turn_index == 1:
            session_title = title or (user_msg[:30] + ("…" if len(user_msg) > 30 else ""))
            with self._lock:
                conn = self._conn()
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO sessions(session_id, user_id, title, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (session_id, user_id, session_title, now, now),
                    )
                    conn.commit()
                finally:
                    conn.close()
        else:
            with self._lock:
                conn = self._conn()
                try:
                    conn.execute(
                        "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                        (now, session_id),
                    )
                    conn.commit()
                finally:
                    conn.close()

    # —— 读取（按时序）——

    def _get_messages(self, session_id: str) -> list[dict[str, Any]]:
        """取一个 session 的全部对话消息，按 turn_index + role 排序。"""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT role, content, turn_index, metadata FROM messages "
                "WHERE session_id = ? ORDER BY turn_index ASC, "
                "CASE role WHEN 'user' THEN 0 ELSE 1 END ASC",
                (session_id,),
            ).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            try:
                meta = json.loads(r["metadata"]) if r["metadata"] else {}
            except (json.JSONDecodeError, TypeError):
                meta = {}
            out.append({
                "role": r["role"],
                "content": r["content"],
                "turn_index": int(r["turn_index"]),
                "metadata": meta,
            })
        return out

    def get_recent(self, session_id: str, n_turns: int) -> list[dict[str, str]]:
        """取最近 n_turns 轮原文消息（每轮 user+assistant 共 2 条）。"""
        all_msgs = self._get_messages(session_id)
        turn_ids = sorted({m["turn_index"] for m in all_msgs})
        recent_turns = set(turn_ids[-n_turns:]) if n_turns > 0 else set()
        return [
            {"role": m["role"], "content": m["content"], "turn_index": m["turn_index"]}
            for m in all_msgs
            if m["turn_index"] in recent_turns
        ]

    def get_all(self, session_id: str) -> list[dict[str, Any]]:
        """取全部对话消息（按时序），每条含 role / content / turn_index / metadata。"""
        return self._get_messages(session_id)

    def get_last_assistant_metadata(self, session_id: str) -> dict[str, Any]:
        """获取最近一条 assistant 消息的 metadata（包含 sources / tool_data 等）。"""
        msgs = self._get_messages(session_id)
        for m in reversed(msgs):
            if m.get("role") == "assistant":
                return m.get("metadata") or {}
        return {}

    def count(self, session_id: str) -> int:
        """消息条数。"""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        return int(row["c"]) if row else 0

    def count_turns(self, session_id: str) -> int:
        """对话轮次数。"""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT COUNT(DISTINCT turn_index) AS c FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        return int(row["c"]) if row else 0

    # —— 关键词检索（替代向量检索）——

    def search(
        self,
        session_id: str,
        query: str,
        k: int = 3,
    ) -> list[dict[str, str]]:
        """关键词模糊匹配历史消息（LIKE），返回最近 k 条。

        语义检索能力弱于 Chroma 向量检索，但满足简单关键词场景。
        """
        like = f"%{query}%"
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT role, content, turn_index FROM messages "
                "WHERE session_id = ? AND content LIKE ? "
                "ORDER BY turn_index DESC LIMIT ?",
                (session_id, like, int(k)),
            ).fetchall()
        finally:
            conn.close()
        out = [
            {
                "role": r["role"],
                "content": r["content"],
                "turn_index": int(r["turn_index"]),
            }
            for r in rows
        ]
        out.sort(key=lambda x: x["turn_index"])
        return out

    # —— 摘要（压缩历史）——

    def get_summary(self, session_id: str) -> tuple[str, int]:
        """读取已压缩摘要。返回 (summary_text, up_to_turn)。无则 ("", 0)。"""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT summary, up_to_turn FROM summaries WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return "", 0
        return row["summary"], int(row["up_to_turn"])

    def set_summary(
        self,
        session_id: str,
        summary: str,
        up_to_turn: int,
    ) -> None:
        """覆盖写入摘要。up_to_turn 表示摘要覆盖到第几轮（含）。"""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO summaries(session_id, summary, up_to_turn, ts) "
                    "VALUES (?, ?, ?, ?)",
                    (session_id, summary, int(up_to_turn), int(time.time())),
                )
                conn.commit()
            finally:
                conn.close()

    # —— 会话列表与归属 ——

    def list_sessions(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """列出某用户的会话（按 updated_at 倒序）。"""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT session_id, user_id, title, created_at, updated_at "
                "FROM sessions WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
                (user_id, int(limit)),
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "session_id": r["session_id"],
                "user_id": r["user_id"],
                "title": r["title"],
                "created_at": int(r["created_at"]),
                "updated_at": int(r["updated_at"]),
            }
            for r in rows
        ]

    def get_session_owner(self, session_id: str) -> str:
        """查询会话归属的 user_id；不存在返回空串。"""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT user_id FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        return row["user_id"] if row else ""

    def rename_session(self, session_id: str, title: str) -> bool:
        """重命名会话标题；返回是否成功（会话存在）。"""
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "UPDATE sessions SET title = ?, updated_at = ? WHERE session_id = ?",
                    (title, int(time.time()), session_id),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    # —— 清理 ——

    def clear_session(self, session_id: str) -> None:
        """删除一个 session 的全部数据（消息 + 摘要 + 会话元信息）。"""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM summaries WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
                conn.commit()
            finally:
                conn.close()


def new_session_id() -> str:
    """生成新的 session id。"""
    return uuid.uuid4().hex
