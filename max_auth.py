from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from config import MAX_DEVICE_ID, MAX_PHONE, MAX_SESSION_DIR, MAX_SESSION_NAME

SMS_CODE_FILE = Path(os.getenv("MAX_SMS_CODE_FILE", "data/.max_sms_code"))
SMS_CODE_POLL_SECONDS = float(os.getenv("MAX_SMS_CODE_POLL_SECONDS", "3"))


def _is_wrong_sms_code_error(exc: BaseException) -> bool:
    error_code = getattr(exc, "error", None)
    if error_code == "verify.code.wrong":
        return True
    return "verify.code.wrong" in str(exc)


class WaitingSmsCodeProvider:
    """Waits for a fresh SMS code from file or terminal."""

    def __init__(self) -> None:
        self._ignored_codes = _collect_ignored_sms_codes()
        self._file_mtime_at_start = _sms_code_file_mtime()

    async def get_code(self, phone: str) -> str:
        SMS_CODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        if os.getenv("MAX_SMS_CODE", "").strip():
            print(
                "Warning: MAX_SMS_CODE is set in .env but is ignored during auth. "
                f"Write the fresh SMS code to {SMS_CODE_FILE} instead, "
                "then remove MAX_SMS_CODE from .env after successful login.",
                flush=True,
            )
        print(
            f"SMS code requested for {phone}.\n"
            f"Write the fresh code to {SMS_CODE_FILE}:\n"
            f"  echo 123456 > {SMS_CODE_FILE}\n"
            "Or enter the code in this terminal if input is available.",
            flush=True,
        )

        stdin_task: asyncio.Task[str] | None = None
        if sys.stdin.isatty():
            stdin_task = asyncio.create_task(
                asyncio.to_thread(input, f"Enter SMS code for {phone}: ")
            )

        try:
            while True:
                code, source = _resolve_fresh_sms_code(
                    ignored_codes=self._ignored_codes,
                    file_mtime_at_start=self._file_mtime_at_start,
                )
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
                        if entered and entered not in self._ignored_codes:
                            print("Using SMS code from terminal", flush=True)
                            return entered
                        stdin_task = asyncio.create_task(
                            asyncio.to_thread(input, f"Enter SMS code for {phone}: ")
                        )
                    elif stdin_task.cancelled():
                        stdin_task = None

                print(
                    f"Waiting for SMS code for {phone}... "
                    f"(write it to {SMS_CODE_FILE})",
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


def _sms_code_file_mtime() -> float:
    try:
        return SMS_CODE_FILE.stat().st_mtime
    except OSError:
        return 0.0


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


def _collect_ignored_sms_codes() -> set[str]:
    ignored: set[str] = set()
    env_code = os.getenv("MAX_SMS_CODE", "").strip()
    if env_code:
        ignored.add(env_code)
    file_code = _read_sms_code_file()
    if file_code:
        ignored.add(file_code)
    return ignored


def _prepare_for_sms_auth() -> None:
    ignored = _collect_ignored_sms_codes()
    if ignored:
        print(
            "Ignoring stale SMS code(s) from previous attempts. "
            f"Wait for a new SMS, then write the fresh code to {SMS_CODE_FILE}.",
            flush=True,
        )
    _clear_sms_code_file()


def _resolve_fresh_sms_code(
    *,
    ignored_codes: set[str],
    file_mtime_at_start: float,
) -> tuple[str | None, str]:
    file_code = _read_sms_code_file()
    if not file_code:
        return None, ""

    file_is_fresh = _sms_code_file_mtime() > file_mtime_at_start
    if file_code in ignored_codes and not file_is_fresh:
        return None, ""

    return file_code, str(SMS_CODE_FILE)


async def authorize_max() -> None:
    if not MAX_PHONE:
        raise RuntimeError("Set MAX_PHONE in .env, for example MAX_PHONE=+79990000000")

    from pymax import Client, ExtraConfig

    MAX_SESSION_DIR.mkdir(parents=True, exist_ok=True)
    client = Client(
        phone=MAX_PHONE,
        session_name=MAX_SESSION_NAME,
        work_dir=str(MAX_SESSION_DIR),
        extra_config=ExtraConfig(device_id=MAX_DEVICE_ID, reconnect=False),
        sms_code_provider=WaitingSmsCodeProvider(),
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
            f"MAX auth complete. user_id={user_id}, session={max_session_path()}",
            flush=True,
        )
        await c.stop()

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
    _prepare_for_sms_auth()

    from pymax.exceptions import ApiError

    while not is_max_authorized():
        try:
            await authorize_max()
        except ApiError as exc:
            if not _is_wrong_sms_code_error(exc):
                raise
            print(
                "Wrong SMS code. Waiting for a new SMS and a fresh code in "
                f"{SMS_CODE_FILE}.",
                flush=True,
            )
            _prepare_for_sms_auth()
            continue
        except Exception:
            if is_max_authorized():
                return
            raise

        if is_max_authorized():
            return


async def main() -> None:
    await ensure_max_session()


if __name__ == "__main__":
    asyncio.run(main())
