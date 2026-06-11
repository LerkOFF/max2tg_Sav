from __future__ import annotations
import asyncio
import aiosqlite
from aiogram import Bot
from config import TG_BOT_TOKEN, TG_GROUP_ID, DB_PATH

async def cleanup():
    bot = Bot(token=TG_BOT_TOKEN)
    async with aiosqlite.connect(DB_PATH) as db:
        # Get all mappings
        async with db.execute("SELECT tg_thread_id, chat_name FROM chat_mapping") as cursor:
            mappings = await cursor.fetchall()
        
        print(f"Найдено {len(mappings)} топиков для удаления...")
        
        for thread_id, name in mappings:
            try:
                print(f"Удаляю топик '{name}' (ID: {thread_id})...")
                await bot.delete_forum_topic(chat_id=TG_GROUP_ID, message_thread_id=thread_id)
            except Exception as e:
                print(f"Ошибка при удалении топика {thread_id}: {e}")
            await asyncio.sleep(0.5) # Avoid rate limits
            
        # Clear DB table
        await db.execute("DELETE FROM chat_mapping")
        await db.commit()
        print("База данных очищена.")
    
    await bot.session.close()

if __name__ == "__main__":
    asyncio.run(cleanup())
