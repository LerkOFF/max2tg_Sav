from __future__ import annotations
import asyncio
import json
from maxapi_bootstrap import bootstrap_maxapi

bootstrap_maxapi()

from max_proto import MaxSdk
from config import AUTH_BUNDLE_PATH

async def check():
    sdk = MaxSdk.from_auth_bundle(AUTH_BUNDLE_PATH)
    try:
        print("Получаю список чатов...")
        res = await asyncio.to_thread(sdk.list_chats)
        # Сохраним в файл для анализа
        with open("chats_debug.json", "w") as f:
            json.dump(res, f, indent=2, ensure_ascii=False)
        
        print(f"Список чатов получен. Всего чатов: {len(res.get('payload', []) if isinstance(res, dict) else res)}")
        print("Результат сохранен в chats_debug.json")
    except Exception as e:
        print(f"Ошибка: {e}")

if __name__ == "__main__":
    asyncio.run(check())
