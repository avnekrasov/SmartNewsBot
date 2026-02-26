from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

import aiosqlite


DB_PATH = "smartnewsbot.db"


@dataclass(slots=True)
class User:
    user_id: int
    send_time: str
    news_limit: int


@dataclass(slots=True)
class UserSource:
    id: int
    user_id: int
    source_url: str
    source_type: str  # 'website' | 'tg_channel'


@dataclass(slots=True)
class UserTopic:
    id: int
    user_id: int
    topic_description: str


class Database:
    """
    Async wrapper around aiosqlite.

    Использует единственное подключение с WAL-режимом для конкурентного чтения
    и последовательной записи без блокировок.
    """

    def __init__(self, db_path: str = DB_PATH) -> None:
        self._db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def init_db(self) -> None:
        """Открыть подключение и создать таблицы."""
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode = WAL;")
        await self._conn.execute("PRAGMA foreign_keys = ON;")

        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                send_time  TEXT NOT NULL DEFAULT '09:00',
                news_limit INTEGER NOT NULL DEFAULT 10
            );

            CREATE TABLE IF NOT EXISTS user_sources (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                source_url  TEXT NOT NULL,
                source_type TEXT NOT NULL CHECK (source_type IN ('website', 'tg_channel')),
                FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS user_topics (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id           INTEGER NOT NULL,
                topic_description TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS interactions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                news_id    TEXT NOT NULL,
                is_liked   INTEGER NOT NULL DEFAULT 0,
                is_clicked INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS sent_news (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                article_id TEXT NOT NULL,
                sent_at    TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_sent_news_user
                ON sent_news (user_id, article_id);
            """
        )
        await self._conn.commit()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call init_db() first.")
        return self._conn

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ── user helpers ──────────────────────────────────────────

    async def upsert_user(
        self, user_id: int, send_time: str = "09:00", news_limit: int = 10
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO users (user_id, send_time, news_limit)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE
                SET send_time  = COALESCE(excluded.send_time,  users.send_time),
                    news_limit = COALESCE(excluded.news_limit, users.news_limit);
            """,
            (user_id, send_time, news_limit),
        )
        await self.conn.commit()

    async def get_user(self, user_id: int) -> Optional[User]:
        cursor = await self.conn.execute(
            "SELECT user_id, send_time, news_limit FROM users WHERE user_id = ?;",
            (user_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return User(
            user_id=row["user_id"],
            send_time=row["send_time"],
            news_limit=row["news_limit"],
        )

    async def list_users(self) -> List[User]:
        cursor = await self.conn.execute(
            "SELECT user_id, send_time, news_limit FROM users;"
        )
        rows = await cursor.fetchall()
        return [
            User(
                user_id=r["user_id"],
                send_time=r["send_time"],
                news_limit=r["news_limit"],
            )
            for r in rows
        ]

    async def update_send_time(self, user_id: int, send_time: str) -> None:
        await self.conn.execute(
            "UPDATE users SET send_time = ? WHERE user_id = ?;",
            (send_time, user_id),
        )
        await self.conn.commit()

    async def update_news_limit(self, user_id: int, news_limit: int) -> None:
        await self.conn.execute(
            "UPDATE users SET news_limit = ? WHERE user_id = ?;",
            (news_limit, user_id),
        )
        await self.conn.commit()

    async def get_user_settings(self, user_id: int) -> Tuple[str, int]:
        """Возвращает (send_time, news_limit). Дефолт: ('09:00', 10)."""
        cursor = await self.conn.execute(
            "SELECT send_time, news_limit FROM users WHERE user_id = ?;",
            (user_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return "09:00", 10
        return row["send_time"], row["news_limit"]

    # ── sources helpers ───────────────────────────────────────

    async def add_source(self, user_id: int, source_url: str, source_type: str) -> int:
        cursor = await self.conn.execute(
            "INSERT INTO user_sources (user_id, source_url, source_type) VALUES (?, ?, ?);",
            (user_id, source_url, source_type),
        )
        await self.conn.commit()
        return cursor.lastrowid or 0

    async def list_sources(self, user_id: int) -> List[UserSource]:
        cursor = await self.conn.execute(
            "SELECT id, user_id, source_url, source_type FROM user_sources WHERE user_id = ? ORDER BY id;",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [
            UserSource(id=r["id"], user_id=r["user_id"], source_url=r["source_url"], source_type=r["source_type"])
            for r in rows
        ]

    async def delete_source(self, source_id: int, user_id: int) -> bool:
        cursor = await self.conn.execute(
            "DELETE FROM user_sources WHERE id = ? AND user_id = ?;",
            (source_id, user_id),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    # ── topics helpers ────────────────────────────────────────

    async def add_topic(self, user_id: int, topic_description: str) -> int:
        cursor = await self.conn.execute(
            "INSERT INTO user_topics (user_id, topic_description) VALUES (?, ?);",
            (user_id, topic_description),
        )
        await self.conn.commit()
        return cursor.lastrowid or 0

    async def list_topics(self, user_id: int) -> List[UserTopic]:
        cursor = await self.conn.execute(
            "SELECT id, user_id, topic_description FROM user_topics WHERE user_id = ? ORDER BY id;",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [
            UserTopic(id=r["id"], user_id=r["user_id"], topic_description=r["topic_description"])
            for r in rows
        ]

    async def delete_topic(self, topic_id: int, user_id: int) -> bool:
        cursor = await self.conn.execute(
            "DELETE FROM user_topics WHERE id = ? AND user_id = ?;",
            (topic_id, user_id),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    # ── interactions helpers ──────────────────────────────────

    async def log_interaction(
        self,
        user_id: int,
        news_id: str,
        *,
        is_liked: bool,
        is_clicked: bool,
    ) -> None:
        await self.conn.execute(
            "INSERT INTO interactions (user_id, news_id, is_liked, is_clicked) VALUES (?, ?, ?, ?);",
            (user_id, news_id, int(is_liked), int(is_clicked)),
        )
        await self.conn.commit()

    async def get_recent_interactions(
        self, user_id: int, limit: int = 100
    ) -> List[Tuple[str, bool, bool]]:
        cursor = await self.conn.execute(
            "SELECT news_id, is_liked, is_clicked FROM interactions WHERE user_id = ? ORDER BY id DESC LIMIT ?;",
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        return [(r["news_id"], bool(r["is_liked"]), bool(r["is_clicked"])) for r in rows]

    # ── sent_news helpers (дедупликация) ──────────────────────

    async def is_news_sent(self, user_id: int, article_id: str) -> bool:
        cursor = await self.conn.execute(
            "SELECT 1 FROM sent_news WHERE user_id = ? AND article_id = ? LIMIT 1;",
            (user_id, article_id),
        )
        return await cursor.fetchone() is not None

    async def mark_news_sent(self, user_id: int, article_id: str) -> None:
        await self.conn.execute(
            "INSERT INTO sent_news (user_id, article_id) VALUES (?, ?);",
            (user_id, article_id),
        )
        await self.conn.commit()

    async def cleanup_old_sent_news(self, days: int = 30) -> None:
        """Удалить записи старше N дней для экономии места."""
        await self.conn.execute(
            "DELETE FROM sent_news WHERE sent_at < datetime('now', ?);",
            (f"-{days} days",),
        )
        await self.conn.commit()


__all__ = [
    "Database",
    "User",
    "UserSource",
    "UserTopic",
    "DB_PATH",
]
