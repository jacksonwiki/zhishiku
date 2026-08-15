"""用户存储：基于 SQLite 的用户表与 bcrypt 密码哈希。

设计：
- users 表：id（uuid）/ username（唯一）/ password_hash / display_name / created_at
- 密码用 bcrypt 加盐哈希，不可逆。
- 访客模式：前端生成 guest_xxx id；登录后用真实 user_id。
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

import bcrypt

from app.config import settings


@dataclass
class User:
    id: str
    username: str
    display_name: str
    created_at: int


@lru_cache(maxsize=1)
def get_user_store() -> "UserStore":
    """返回用户存储单例（与 chat.db 共用同一 SQLite 文件）。"""
    return UserStore(str(settings.chat_db))


class UserStore:
    """用户表 CRUD + 密码哈希校验。

    线程安全：写操作加 Lock；读用独立连接。
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        id            TEXT PRIMARY KEY,
                        username      TEXT UNIQUE NOT NULL,
                        password_hash TEXT NOT NULL,
                        display_name  TEXT NOT NULL,
                        created_at    INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
                    """
                )
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _hash_password(password: str) -> str:
        """bcrypt 加盐哈希（cost=12）。"""
        salt = bcrypt.gensalt(rounds=12)
        return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")

    @staticmethod
    def _verify_password(password: str, password_hash: str) -> bool:
        """校验密码与哈希是否匹配。"""
        try:
            return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
        except Exception:
            return False

    def create_user(self, username: str, password: str, display_name: Optional[str] = None) -> User:
        """注册新用户。

        Raises:
            ValueError: 用户名已存在。
        """
        if not username or len(username) < 2:
            raise ValueError("用户名至少 2 个字符")
        if len(password) < 4:
            raise ValueError("密码至少 4 个字符")
        display_name = display_name or username
        user_id = uuid.uuid4().hex
        pwd_hash = self._hash_password(password)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO users(id, username, password_hash, display_name, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (user_id, username, pwd_hash, display_name, int(time.time())),
                )
                conn.commit()
            except sqlite3.IntegrityError as e:
                raise ValueError(f"用户名 {username!r} 已存在") from e
            finally:
                conn.close()
        return User(id=user_id, username=username, display_name=display_name, created_at=int(time.time()))

    def authenticate(self, username: str, password: str) -> Optional[User]:
        """用户名 + 密码校验；成功返回 User，失败返回 None。"""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT id, username, password_hash, display_name, created_at FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        if not self._verify_password(password, row["password_hash"]):
            return None
        return User(
            id=row["id"],
            username=row["username"],
            display_name=row["display_name"],
            created_at=int(row["created_at"]),
        )

    def get_user(self, user_id: str) -> Optional[User]:
        """按 id 查询用户。"""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT id, username, display_name, created_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        return User(
            id=row["id"],
            username=row["username"],
            display_name=row["display_name"],
            created_at=int(row["created_at"]),
        )


def new_guest_id() -> str:
    """生成访客 id（前端也可生成；这里供后端兜底）。"""
    return f"guest_{uuid.uuid4().hex[:16]}"
