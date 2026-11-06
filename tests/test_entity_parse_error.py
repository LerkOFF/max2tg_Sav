from __future__ import annotations

import os
import unittest

os.environ.setdefault("TG_BOT_TOKEN", "123:abc")


class TestEntityParseError(unittest.TestCase):
    def test_detects_html_parse_errors_only(self) -> None:
        from main import _is_tg_entity_parse_error

        self.assertTrue(_is_tg_entity_parse_error(Exception("Bad Request: can't parse entities: Unsupported start tag")))
        self.assertTrue(_is_tg_entity_parse_error(Exception("Bad Request: can't find end of the entity starting at byte offset 5")))
        self.assertFalse(_is_tg_entity_parse_error(Exception("Bad Request: message thread not found")))


if __name__ == "__main__":
    unittest.main()
