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


class WaitingSmsCodeProvider:
    """Waits for SMS code from env, file, or terminal without crashing on EOF."""

    async def get_code(self, phone: str) -> str:
        SMS_CODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        print(
            f"SMS code requested for {phone}.\n"
            f"Provide it in one of these ways:\n"
            f"  1. Write the code to {SMS_CODE_FILE}\n"
            f"  2. Set MAX_SMS_CODE in .env and restart the container\n"
            f"  3. Enter the code in this terminal if input is available",
            flush=True,
        )

        stdin_task: asyncio.Task[str] | None = None
        if sys.stdin.isatty():
            stdin_task = asyncio.create_task(
                asyncio.to_thread(input, f"Enter SMS code for {phone}: ")
            )

        try:
            while True:
                code, source = _resolve_sms_code()
                if code:
                    print(f"Using SMS code from {source}", flush=True)
                    if source == str(SMS_CODE_FILE):
                        _clear_sms_code_file()
                    return code

                if stdin_task is not None:
                    if stdin_task.done():
                        try:
                            entered = stdin_task.result().strip()
                        except Exception as exc:
                            raise RuntimeError("Failed to read SMS code from terminal") from exc
                        if entered:
                            print("Using SMS code from terminal", flush=True)
                            return entered
                        stdin_task = asyncio.create_task(
                            asyncio.to_thread(input, f"Enter SMS code for {phone}: ")
                        )
                    elif stdin_task.cancelled():
                        stdin_task = None

                print(
                    f"Waiting for SMS code for {phone}... "
                    f"(write it to {SMS_CODE_FILE} or enter in terminal)",
                    flush=True,
                )
                await asyncio.sleep(SMS_CODE_POLL_SECONDS)
        finally:
            if stdin_task is not None and not stdin_task.done():
                stdin_task.cancel()


def max_session_path() -> Path:
    return MAX_SESSION_DIR / MAX_SESSION_NAME


def is_max_session_usable() -> bool:
    path = max_session_path()
    if not path.exists():
        return False
    import sqlite3

    try:
        with sqlite3.connect(path) as conn:
            row = conn.execute("SELECT token FROM sessions LIMIT 1").fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row[0])


def remove_stale_max_session() -> None:
    path = max_session_path()
    if path.exists():
        path.unlink(missing_ok=True)


def is_max_authorized() -> bool:
    return is_max_session_usable()


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


async def authorize_max(*, sms_code: str | None = None, source: str = "waiting") -> None:
    if not MAX_PHONE:
        raise RuntimeError("Set MAX_PHONE in .env, for example MAX_PHONE=+79990000000")

    from pymax import Client, ExtraConfig

    MAX_SESSION_DIR.mkdir(parents=True, exist_ok=True)
    if sms_code:
        provider = EnvSmsCodeProvider(sms_code)
    else:
        provider = WaitingSmsCodeProvider()

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
        if not auth_completed and not is_max_session_usable():
            raise


async def ensure_max_session() -> None:
    if is_max_authorized():
        print(f"MAX session found: {max_session_path()}", flush=True)
        return

    if max_session_path().exists():
        print(
            "MAX session file exists but contains no token; re-authorization required...",
            flush=True,
        )
        remove_stale_max_session()

    print("MAX session not found. Starting authorization...", flush=True)

    sms_code, source = _resolve_sms_code()
    await authorize_max(
        sms_code=sms_code,
        source=source if sms_code else "waiting",
    )
    if sms_code and source == str(SMS_CODE_FILE):
        _clear_sms_code_file()


async def main() -> None:
    await ensure_max_session()


if __name__ == "__main__":
    asyncio.run(main())
