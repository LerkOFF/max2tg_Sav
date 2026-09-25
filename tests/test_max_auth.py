from __future__ import annotations

import asyncio
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from max_auth import (
    _prepare_for_sms_auth,
    _resolve_fresh_sms_code,
    WaitingPasswordProvider,
    is_max_session_usable,
    remove_stale_max_session,
    submit_password,
)


class TestMaxAuth(unittest.TestCase):
    def test_empty_session_file_is_not_usable(self) -> None:
        tmpdir = tempfile.mkdtemp()
        try:
            session_dir = Path(tmpdir)
            session_path = session_dir / "session.db"
            conn = sqlite3.connect(session_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE sessions (
                        token TEXT NOT NULL PRIMARY KEY,
                        device_id TEXT NOT NULL,
                        phone TEXT NOT NULL
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

            with patch("max_auth.MAX_SESSION_DIR", session_dir), patch(
                "max_auth.MAX_SESSION_NAME", "session.db"
            ):
                self.assertFalse(is_max_session_usable())
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_session_with_token_is_usable(self) -> None:
        tmpdir = tempfile.mkdtemp()
        try:
            session_dir = Path(tmpdir)
            session_path = session_dir / "session.db"
            conn = sqlite3.connect(session_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE sessions (
                        token TEXT NOT NULL PRIMARY KEY,
                        device_id TEXT NOT NULL,
                        phone TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO sessions (token, device_id, phone) VALUES (?, ?, ?)",
                    ("token-123", "device", "+79990000000"),
                )
                conn.commit()
            finally:
                conn.close()

            with patch("max_auth.MAX_SESSION_DIR", session_dir), patch(
                "max_auth.MAX_SESSION_NAME", "session.db"
            ):
                self.assertTrue(is_max_session_usable())
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_remove_stale_max_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir)
            session_path = session_dir / "session.db"
            session_path.write_text("stale", encoding="utf-8")

            with patch("max_auth.MAX_SESSION_DIR", session_dir), patch(
                "max_auth.MAX_SESSION_NAME", "session.db"
            ):
                remove_stale_max_session()
                self.assertFalse(session_path.exists())

    def test_ignores_stale_file_code_until_file_is_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            code_file = Path(tmpdir) / ".max_sms_code"
            code_file.write_text("805019", encoding="utf-8")
            mtime = code_file.stat().st_mtime

            with patch("max_auth.SMS_CODE_FILE", code_file):
                code, source = _resolve_fresh_sms_code(
                    ignored_codes={"805019"},
                    file_mtime_at_start=mtime,
                )
                self.assertIsNone(code)

                code_file.write_text("123456", encoding="utf-8")
                code, source = _resolve_fresh_sms_code(
                    ignored_codes={"805019"},
                    file_mtime_at_start=mtime,
                )
                self.assertEqual(code, "123456")
                self.assertEqual(source, str(code_file))

    def test_prepare_for_sms_auth_clears_code_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            code_file = Path(tmpdir) / ".max_sms_code"
            code_file.write_text("805019", encoding="utf-8")

            with patch("max_auth.SMS_CODE_FILE", code_file):
                _prepare_for_sms_auth()
                self.assertFalse(code_file.exists())

    def test_extract_sms_code_only_digits(self) -> None:
        from max_auth import extract_sms_code

        self.assertEqual(extract_sms_code("504310"), "504310")
        self.assertEqual(extract_sms_code(" 504 310 "), "504310")
        self.assertIsNone(extract_sms_code("код 504310"))
        self.assertIsNone(extract_sms_code(""))
        self.assertIsNone(extract_sms_code(None))

    def test_stale_session_error_detects_login_token(self) -> None:
        from max_auth import is_stale_session_error, submit_sms_code

        class FakeApiError(Exception):
            error = "login.token"

        self.assertTrue(is_stale_session_error(FakeApiError("FAIL_LOGIN_TOKEN")))
        self.assertTrue(is_stale_session_error(RuntimeError("Not connected to the server")))
        self.assertFalse(is_stale_session_error(RuntimeError("flood wait")))
        self.assertFalse(submit_sms_code("123456"))


class TestPasswordAuth(unittest.IsolatedAsyncioTestCase):
    async def test_password_is_requested_privately_and_only_sms_sender_can_submit(self) -> None:
        notifications: list[tuple[str, str]] = []

        async def notify(kind: str, hint: str) -> None:
            notifications.append((kind, hint))

        with patch("max_auth._password_user_id", 42), patch(
            "max_auth._password_attempts", 0
        ), patch("max_auth._sms_request_notifier", notify):
            task = asyncio.create_task(WaitingPasswordProvider().get_password("hint"))
            await asyncio.sleep(0)
            self.assertEqual(notifications, [("password", "hint")])
            self.assertFalse(submit_password("wrong sender", 43))
            self.assertTrue(submit_password("secret", 42))
            self.assertEqual(await task, "secret")
            self.assertFalse(submit_password("too late", 42))

            retry = asyncio.create_task(WaitingPasswordProvider().get_password())
            await asyncio.sleep(0)
            self.assertEqual(notifications[-1], ("password_retry", ""))
            self.assertTrue(submit_password("corrected", 42))
            self.assertEqual(await retry, "corrected")


if __name__ == "__main__":
    unittest.main()
