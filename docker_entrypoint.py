from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def validate_bridge_config() -> None:
    from config import MAX_PHONE, TG_BOT_TOKEN, TG_GROUP_ID

    errors: list[str] = []
    placeholders = {"", "your_bot_token_here", "0"}

    if not TG_BOT_TOKEN or TG_BOT_TOKEN in placeholders:
        errors.append("TG_BOT_TOKEN is missing in .env")
    if TG_GROUP_ID == 0:
        errors.append("TG_GROUP_ID is missing or invalid in .env")
    if not MAX_PHONE:
        errors.append("MAX_PHONE is missing in .env")

    if errors:
        message = "Bridge configuration is incomplete:\n" + "\n".join(f"- {item}" for item in errors)
        message += "\n\nCopy .env.example to .env and fill in the values before starting Docker."
        raise RuntimeError(message)


def prepare_runtime_dirs() -> None:
    for path in (Path("data"), Path("logs"), Path("data/pymax")):
        path.mkdir(parents=True, exist_ok=True)


async def bootstrap() -> None:
    validate_bridge_config()
    prepare_runtime_dirs()

    from max_auth import ensure_max_session

    await ensure_max_session()


def start_bridge() -> None:
    os.execvp(sys.executable, [sys.executable, "-u", "main.py"])


def main() -> None:
    try:
        asyncio.run(bootstrap())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        import traceback

        print(f"Startup failed: {exc}", file=sys.stderr, flush=True)
        traceback.print_exc()
        raise SystemExit(1) from exc
    start_bridge()


if __name__ == "__main__":
    main()
