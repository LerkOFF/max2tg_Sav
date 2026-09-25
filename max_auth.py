from __future__ import annotations

import asyncio
import os
import re
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from config import MAX_DEVICE_ID, MAX_PHONE, MAX_SESSION_DIR, MAX_SESSION_NAME

SMS_CODE_FILE = Path(os.getenv("MAX_SMS_CODE_FILE", "data/.max_sms_code"))
SMS_CODE_POLL_SECONDS = float(os.getenv("MAX_SMS_CODE_POLL_SECONDS", "3"))
SMS_CODE_RE = re.compile(r"^\d{4,8}$")

SmsRequestNotifier = Callable[[str, str], Awaitable[None]]

_sms_request_notifier: SmsRequestNotifier | None = None
_sms_code_queue: asyncio.Queue[str] | None = None
_waiting_for_sms = False
_sms_retry = False
_password_queue: asyncio.Queue[str] | None = None
_waiting_for_password = False
_password_user_id: int | None = None
_password_attempts = 0


def set_sms_request_notifier(notifier: SmsRequestNotifier | None) -> None:
    global _sms_request_notifier
    _sms_request_notifier = notifier


def is_waiting_for_sms_code() -> bool:
    return _waiting_for_sms


def is_waiting_for_password() -> bool:
    return _waiting_for_password


def password_recipient_id() -> int | None:
    return _password_user_id


def extract_sms_code(text: str | None) -> str | None:
    if not text:
        return None
    code = re.sub(r"\s+", "", text.strip())
    if SMS_CODE_RE.fullmatch(code):
        return code
    return None


def submit_sms_code(code: str, user_id: int | None = None) -> bool:
    global _password_user_id
    if _sms_code_queue is None:
        return False
    cleaned = (code or "").strip()
    if not cleaned:
        return False
    _password_user_id = user_id
    _sms_code_queue.put_nowait(cleaned)
    return True


def submit_password(password: str, user_id: int) -> bool:
    if _password_queue is None or not _waiting_for_password:
        return False
    if _password_user_id is not None and user_id != _password_user_id:
        return False
    if not password:
        return False
    _password_queue.put_nowait(password)
    return True


def _exc_blob(exc: BaseException) -> str:
    error_code = getattr(exc, "error", None)
    return f"{error_code or ''} {exc}".lower()


def _is_wrong_sms_code_error(exc: BaseException) -> bool:
    blob = _exc_blob(exc)
    return "verify.code.wrong" in blob or "error.code.attempt.limit" in blob or "код устарел" in blob


def is_stale_session_error(exc: BaseException) -> bool:
    blob = _exc_blob(exc)
    return any(
        token in blob
        for token in (
            "login.token",
            "fail_login_token",
            "not connected to the server",
            "transport is not connected",
        )
    )


class WaitingSmsCodeProvider:
    """Waits for a fresh SMS code from file or terminal."""

    def __init__(self) -> None:
        self._ignored_codes = _collect_ignored_sms_codes()
        self._file_mtime_at_start = _sms_code_file_mtime()

    async def get_code(self, phone: str) -> str:
        global _sms_code_queue, _waiting_for_sms, _sms_retry

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
            f"Send it in Telegram General topic /1, or write it to {SMS_CODE_FILE}:\n"
            f"  echo 123456 > {SMS_CODE_FILE}",
            flush=True,
        )

        _sms_code_queue = asyncio.Queue()
        _waiting_for_sms = True
        if _sms_request_notifier is not None:
            kind = "expired" if _sms_retry else "requested"
            await _sms_request_notifier(kind, phone)
        _sms_retry = False

        stdin_task: asyncio.Task[str] | None = None
        use_stdin = sys.stdin.isatty() and os.getenv("MAX_SMS_USE_STDIN", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        if use_stdin:
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

                try:
                    queued = await asyncio.wait_for(
                        _sms_code_queue.get(),
                        timeout=SMS_CODE_POLL_SECONDS,
                    )
                except TimeoutError:
                    queued = None
                if queued:
                    print("Using SMS code from Telegram General topic", flush=True)
                    return queued

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
                    f"(Telegram General /1 or {SMS_CODE_FILE})",
                    flush=True,
                )
        finally:
            _waiting_for_sms = False
            _sms_code_queue = None
            if stdin_task is not None and not stdin_task.done():
                stdin_task.cancel()


class WaitingPasswordProvider:
    """Wait for the MAX 2FA password in a private Telegram chat."""

    async def get_password(self, hint: str | None = None) -> str:
        global _password_queue, _waiting_for_password, _password_attempts

        _password_queue = asyncio.Queue()
        _waiting_for_password = True
        kind = "password_retry" if _password_attempts else "password"
        _password_attempts += 1
        try:
            if _sms_request_notifier is not None:
                await _sms_request_notifier(kind, hint or "")
            return await _password_queue.get()
        finally:
            _waiting_for_password = False
            _password_queue = None


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
        password_provider=WaitingPasswordProvider(),
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
    global _sms_retry, _password_user_id, _password_attempts

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
    _password_user_id = None
    _password_attempts = 0
    _prepare_for_sms_auth()

    from pymax.exceptions import ApiError

    while not is_max_authorized():
        try:
            await authorize_max()
        except ApiError as exc:
            if not _is_wrong_sms_code_error(exc):
                raise
            print(
                "SMS code rejected by MAX; requesting a new one after the submitted code.",
                flush=True,
            )
            _sms_retry = True
            _password_user_id = None
            _password_attempts = 0
            _prepare_for_sms_auth()
            continue
        except Exception:
            if is_max_authorized():
                break
            raise

        if is_max_authorized():
            break

    if _sms_request_notifier is not None:
        await _sms_request_notifier("success", MAX_PHONE)


async def main() -> None:
    await ensure_max_session()


if __name__ == "__main__":
    asyncio.run(main())
