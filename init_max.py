import asyncio

from max_auth import ensure_max_session


async def main() -> None:
    await ensure_max_session()


if __name__ == "__main__":
    asyncio.run(main())
