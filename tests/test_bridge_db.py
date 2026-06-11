from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from database import BridgeDB


class TestBridgeDB(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "bridge.db"
        self.db = BridgeDB(str(self.db_path))
        await self.db.init()

    async def asyncTearDown(self) -> None:
        self.tmpdir.cleanup()

    async def test_message_mapping_roundtrip(self) -> None:
        await self.db.save_message_mapping(
            max_chat_id=10,
            max_message_id=20,
            tg_chat_id=30,
            tg_thread_id=40,
            tg_message_id=50,
        )

        tg_messages = await self.db.get_tg_messages_for_max_message(10, 20)
        self.assertEqual(
            tg_messages,
            [{"tg_chat_id": 30, "tg_thread_id": 40, "tg_message_id": 50}],
        )

        max_message = await self.db.get_max_message_for_tg_message(30, 50)
        self.assertEqual(
            max_message,
            {"max_chat_id": 10, "max_message_id": 20, "tg_thread_id": 40},
        )

    async def test_replace_max_message_mappings(self) -> None:
        await self.db.replace_max_message_mappings(
            max_chat_id=100,
            max_message_id=200,
            tg_chat_id=300,
            tg_thread_id=400,
            tg_message_ids=[1, 2, 3],
        )
        await self.db.replace_max_message_mappings(
            max_chat_id=100,
            max_message_id=200,
            tg_chat_id=300,
            tg_thread_id=400,
            tg_message_ids=[7, 8],
        )

        tg_messages = await self.db.get_tg_messages_for_max_message(100, 200)
        self.assertEqual(
            tg_messages,
            [
                {"tg_chat_id": 300, "tg_thread_id": 400, "tg_message_id": 7},
                {"tg_chat_id": 300, "tg_thread_id": 400, "tg_message_id": 8},
            ],
        )

    async def test_delete_message_mappings(self) -> None:
        await self.db.save_message_mapping(
            max_chat_id=11,
            max_message_id=22,
            tg_chat_id=33,
            tg_thread_id=44,
            tg_message_id=55,
        )
        await self.db.delete_max_message_mappings(11, 22)
        self.assertEqual(await self.db.get_tg_messages_for_max_message(11, 22), [])


if __name__ == "__main__":
    unittest.main()
