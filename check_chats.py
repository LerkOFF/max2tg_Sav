import asyncio

from config import MAX_DEVICE_ID, MAX_PHONE, MAX_SESSION_DIR, MAX_SESSION_NAME


async def main() -> None:
    if not MAX_PHONE:
        raise RuntimeError("Set MAX_PHONE in .env")

    from pymax import Client, ExtraConfig

    client = Client(
        phone=MAX_PHONE,
        session_name=MAX_SESSION_NAME,
        work_dir=str(MAX_SESSION_DIR),
        extra_config=ExtraConfig(device_id=MAX_DEVICE_ID, reconnect=False),
    )

    @client.on_start()
    async def on_start(c):
        chats = await c.fetch_chats()
        for chat in chats or []:
            title = getattr(chat, "title", None) or getattr(chat, "type", "")
            print(f"{chat.id}\t{title}")
        await c.stop()

    await client.start()


if __name__ == "__main__":
    asyncio.run(main())
