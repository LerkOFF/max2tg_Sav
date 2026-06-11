from __future__ import annotations
import aiosqlite

from pathlib import Path

class BridgeDB:
    def __init__(self, db_path: str = "data/bridge.db"):
        self.db_path = db_path

    async def init(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS chat_mapping (
                    max_chat_id INTEGER PRIMARY KEY,
                    tg_thread_id INTEGER NOT NULL,
                    chat_name TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS auth_state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS message_mapping (
                    max_chat_id INTEGER NOT NULL,
                    max_message_id INTEGER NOT NULL,
                    tg_chat_id INTEGER NOT NULL,
                    tg_thread_id INTEGER NOT NULL,
                    tg_message_id INTEGER NOT NULL,
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                    PRIMARY KEY (max_chat_id, max_message_id, tg_chat_id, tg_message_id),
                    UNIQUE (tg_chat_id, tg_message_id)
                )
            """)
            await db.commit()

    async def get_thread_id(self, max_chat_id: int) -> int | None:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT tg_thread_id FROM chat_mapping WHERE max_chat_id = ?", (max_chat_id,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None

    async def get_max_chat_id(self, tg_thread_id: int) -> int | None:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT max_chat_id FROM chat_mapping WHERE tg_thread_id = ?", (tg_thread_id,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None

    async def save_mapping(self, max_chat_id: int, tg_thread_id: int, chat_name: str = ""):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO chat_mapping (max_chat_id, tg_thread_id, chat_name) VALUES (?, ?, ?)",
                (max_chat_id, tg_thread_id, chat_name)
            )
            await db.commit()

    async def save_message_mapping(
        self,
        *,
        max_chat_id: int,
        max_message_id: int,
        tg_chat_id: int,
        tg_thread_id: int,
        tg_message_id: int,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO message_mapping
                (max_chat_id, max_message_id, tg_chat_id, tg_thread_id, tg_message_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (max_chat_id, max_message_id, tg_chat_id, tg_thread_id, tg_message_id),
            )
            await db.commit()

    async def replace_max_message_mappings(
        self,
        *,
        max_chat_id: int,
        max_message_id: int,
        tg_chat_id: int,
        tg_thread_id: int,
        tg_message_ids: list[int],
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM message_mapping WHERE max_chat_id = ? AND max_message_id = ?",
                (max_chat_id, max_message_id),
            )
            for tg_message_id in tg_message_ids:
                await db.execute(
                    """
                    INSERT OR REPLACE INTO message_mapping
                    (max_chat_id, max_message_id, tg_chat_id, tg_thread_id, tg_message_id)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (max_chat_id, max_message_id, tg_chat_id, tg_thread_id, tg_message_id),
                )
            await db.commit()

    async def get_tg_messages_for_max_message(self, max_chat_id: int, max_message_id: int) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT tg_chat_id, tg_thread_id, tg_message_id
                FROM message_mapping
                WHERE max_chat_id = ? AND max_message_id = ?
                ORDER BY tg_message_id
                """,
                (max_chat_id, max_message_id),
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            {"tg_chat_id": row[0], "tg_thread_id": row[1], "tg_message_id": row[2]}
            for row in rows
        ]

    async def get_max_message_for_tg_message(self, tg_chat_id: int, tg_message_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT max_chat_id, max_message_id, tg_thread_id
                FROM message_mapping
                WHERE tg_chat_id = ? AND tg_message_id = ?
                """,
                (tg_chat_id, tg_message_id),
            ) as cursor:
                row = await cursor.fetchone()
        if not row:
            return None
        return {"max_chat_id": row[0], "max_message_id": row[1], "tg_thread_id": row[2]}

    async def delete_max_message_mappings(self, max_chat_id: int, max_message_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM message_mapping WHERE max_chat_id = ? AND max_message_id = ?",
                (max_chat_id, max_message_id),
            )
            await db.commit()

    async def delete_tg_message_mapping(self, tg_chat_id: int, tg_message_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM message_mapping WHERE tg_chat_id = ? AND tg_message_id = ?",
                (tg_chat_id, tg_message_id),
            )
            await db.commit()

    async def count_message_mappings_for_chat(self, max_chat_id: int) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM message_mapping WHERE max_chat_id = ?",
                (max_chat_id,),
            ) as cursor:
                row = await cursor.fetchone()
        return int(row[0] or 0) if row else 0

    async def set_auth_data(self, key: str, value: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT OR REPLACE INTO auth_state (key, value) VALUES (?, ?)", (key, value))
            await db.commit()

    async def get_auth_data(self, key: str) -> str | None:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT value FROM auth_state WHERE key = ?", (key,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None
