from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from config import MAX_DEVICE_ID, MAX_PHONE, MAX_SESSION_DIR, MAX_SESSION_NAME

SMS_CODE_FILE = Path(os.getenv("MAX_SMS_CODE_FILE", "data/.max_sms_code"))
SMS_CODE_POLL_SECONDS = float(os.getenv("MAX_SMS_CODE_POLL_SECONDS", "3"))


class EnvSmsCodeProvider:
    def __init__(self, code: str) -> None:
        self._code = code.strip()

    async def get_code(self, phone: str) -> str:
        if not self._code:
            raise RuntimeError("MAX SMS code is empty")
        return self._code


def max_session_path() -> Path:
    return MAX_SESSION_DIR / MAX_SESSION_NAME


def is_max_authorized() -> bool:
    return max_session_path().exists()


def _read_sms_code_file() -> str | None:
    if not SMS_CODE_FILE.exists():
        return None
    try:
        code = SMS_CODE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not code:
        return None
    return code


def _clear_sms_code_file() -> None:
    try:
        SMS_CODE_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _resolve_sms_code() -> tuple[str | None, str]:
    env_code = os.getenv("MAX_SMS_CODE", "").strip()
    if env_code:
        return env_code, "MAX_SMS_CODE"
    file_code = _read_sms_code_file()
    if file_code:
        return file_code, str(SMS_CODE_FILE)
    return None, ""


async def authorize_max(*, sms_code: str | None = None, source: str = "interactive") -> None:
    if not MAX_PHONE:
        raise RuntimeError("Set MAX_PHONE in .env, for example MAX_PHONE=+79990000000")

    from pymax import Client, ConsoleSmsCodeProvider, ExtraConfig

    MAX_SESSION_DIR.mkdir(parents=True, exist_ok=True)
    provider = EnvSmsCodeProvider(sms_code) if sms_code else ConsoleSmsCodeProvider()
    client = Client(
        phone=MAX_PHONE,
        session_name=MAX_SESSION_NAME,
        work_dir=str(MAX_SESSION_DIR),
        extra_config=ExtraConfig(device_id=MAX_DEVICE_ID, reconnect=False),
        sms_code_provider=provider,
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
        print(
            f"MAX auth complete via {source}. "
            f"user_id={user_id}, session={max_session_path()}",
            flush=True,
        )
        await c.stop()

    session_path = max_session_path()
    try:
        await client.start()
    except (asyncio.CancelledError, Exception):
        if not auth_completed and not session_path.exists():
            raise


async def ensure_max_session() -> None:
    if is_max_authorized():
        print(f"MAX session found: {max_session_path()}", flush=True)
        return

    print("MAX session not found. Starting authorization...", flush=True)

    sms_code, source = _resolve_sms_code()
    if sms_code:
        await authorize_max(sms_code=sms_code, source=source)
        if source == str(SMS_CODE_FILE):
            _clear_sms_code_file()
        return

    if sys.stdin.isatty():
        await authorize_max(source="console")
        return

    SMS_CODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    print(
        "Waiting for MAX SMS code.\n"
        f"- write the code to {SMS_CODE_FILE}, or\n"
        "- set MAX_SMS_CODE in .env and restart the container, or\n"
        "- run `docker compose up` in the foreground and enter the code in the terminal.",
        flush=True,
    )
    while not is_max_authorized():
        sms_code, source = _resolve_sms_code()
        if sms_code:
            await authorize_max(sms_code=sms_code, source=source)
            if source == str(SMS_CODE_FILE):
                _clear_sms_code_file()
            return
        await asyncio.sleep(SMS_CODE_POLL_SECONDS)


async def main() -> None:
    await ensure_max_session()


if __name__ == "__main__":
    asyncio.run(main())
