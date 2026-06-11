from __future__ import annotations

import asyncio
import os

import aiosqlite
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from config import TG_GROUP_ID
from database import BridgeDB
from main import (
    bot,
    get_chat_title,
    is_supported_chat,
    max_bridge,
    refresh_user_names_from_login_payload,
    replay_recent_messages_to_topic,
)

DELETE_THREAD_ID_MAX = int(os.getenv("DELETE_THREAD_ID_MAX", "2500"))
DELETE_THREAD_ID_RANGE = range(1, DELETE_THREAD_ID_MAX + 1)
REPLAY_MESSAGES_PER_CHAT = int(os.getenv("REPLAY_MESSAGES_PER_CHAT", os.getenv("BACKFILL_MESSAGES_PER_CHAT", "5")))


async def delete_all_topics(db: BridgeDB) -> list[int]:
    thread_ids = set(DELETE_THREAD_ID_RANGE)
    async with aiosqlite.connect(db.db_path) as conn:
        async with conn.execute("SELECT tg_thread_id FROM chat_mapping") as cursor:
            for (thread_id,) in await cursor.fetchall():
                thread_ids.add(int(thread_id))

    deleted: list[int] = []
    for thread_id in sorted(thread_ids):
        while True:
            try:
                await bot.delete_forum_topic(chat_id=TG_GROUP_ID, message_thread_id=thread_id)
                deleted.append(thread_id)
                print(f"Deleted topic thread_id={thread_id}", flush=True)
                await asyncio.sleep(0.15)
                break
            except TelegramRetryAfter as exc:
                wait_seconds = int(exc.retry_after) + 1
                print(f"Flood control on delete_forum_topic, sleeping {wait_seconds}s", flush=True)
                await asyncio.sleep(wait_seconds)
            except TelegramBadRequest:
                break
    return deleted


async def create_topic_with_retry(title: str):
    while True:
        try:
            return await bot.create_forum_topic(TG_GROUP_ID, title)
        except TelegramRetryAfter as exc:
            wait_seconds = int(exc.retry_after) + 1
            print(f"Flood control on create_forum_topic '{title}', sleeping {wait_seconds}s")
            await asyncio.sleep(wait_seconds)


async def rebuild_topics() -> None:
    db = BridgeDB()
    await db.init()
    if not max_bridge.load_sdk():
        raise RuntimeError("Failed to initialize Max SDK from auth bundle")
    refresh_user_names_from_login_payload()

    try:
        deleted = await delete_all_topics(db)
        print(f"Deleted {len(deleted)} Telegram topics")

        async with aiosqlite.connect(db.db_path) as conn:
            await conn.execute("DELETE FROM chat_mapping")
            await conn.execute("DELETE FROM message_mapping")
            await conn.commit()

        chats = await max_bridge.list_chats()
        supported_chats = [chat for chat in chats if is_supported_chat(chat, chat.get("id"))]
        replay_label = "all available" if REPLAY_MESSAGES_PER_CHAT <= 0 else str(REPLAY_MESSAGES_PER_CHAT)
        print(f"Recreating {len(supported_chats)} topics; replay_messages_per_chat={replay_label}")

        for chat in supported_chats:
            chat_id = chat.get("id")
            if chat_id is None:
                continue
            title = get_chat_title(chat, chat_id)
            topic = await create_topic_with_retry(title)
            thread_id = topic.message_thread_id
            await db.save_mapping(chat_id, thread_id, title)
            print(f"Created topic '{title}' thread_id={thread_id} for chat_id={chat_id}")

            try:
                await replay_recent_messages_to_topic(chat_id, thread_id, count=REPLAY_MESSAGES_PER_CHAT)
            except Exception as exc:
                print(f"Replay failed for chat_id={chat_id}: {exc!r}")
            await asyncio.sleep(0.5)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(rebuild_topics())
