from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from max_auth import is_max_session_usable, remove_stale_max_session


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


if __name__ == "__main__":
    unittest.main()
