import asyncio

from config import MAX_DEVICE_ID, MAX_PHONE, MAX_SESSION_DIR, MAX_SESSION_NAME


async def main() -> None:
    if not MAX_PHONE:
        raise RuntimeError("Set MAX_PHONE in .env, for example MAX_PHONE=+79990000000")

    from pymax import Client, ExtraConfig

    MAX_SESSION_DIR.mkdir(parents=True, exist_ok=True)
    client = Client(
        phone=MAX_PHONE,
        session_name=MAX_SESSION_NAME,
        work_dir=str(MAX_SESSION_DIR),
        extra_config=ExtraConfig(device_id=MAX_DEVICE_ID, reconnect=False),
    )

    auth_completed = False

    @client.on_start()
    async def on_start(c):
        nonlocal auth_completed
        auth_completed = True
        me = c.me
        user_id = (
            getattr(me, "id", None)
            or getattr(me, "contact_id", None)
            or getattr(me, "user_id", None)
        ) if me is not None else None
        print(f"MAX auth complete. user_id={user_id}, session={MAX_SESSION_DIR / MAX_SESSION_NAME}")
        await c.stop()

    session_path = MAX_SESSION_DIR / MAX_SESSION_NAME
    try:
        await client.start()
    except (asyncio.CancelledError, Exception):
        if not auth_completed and not session_path.exists():
            raise


if __name__ == "__main__":
    asyncio.run(main())
