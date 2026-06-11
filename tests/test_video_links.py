from __future__ import annotations

import unittest

from main import (
    _append_video_fallback_note,
    _build_max_video_link_text,
    _max_message_state_emoji,
    _max_message_viewed_by_other_participant,
    _max_reaction_info_emoji,
)


class TestMaxVideoLinks(unittest.TestCase):
    def test_build_video_link_text_uses_external_and_selected_urls(self) -> None:
        html_text, plain_text = _build_max_video_link_text(
            {
                "external_url": "https://max.example/watch?id=1&from=chat",
                "selected_url": "https://cdn.example/video.mp4",
                "sources": {"MP4_360": "https://cdn.example/low.mp4"},
            }
        )

        self.assertIn('href="https://max.example/watch?id=1&amp;from=chat"', html_text)
        self.assertIn('href="https://cdn.example/video.mp4"', html_text)
        self.assertIn("открыть в Max: https://max.example/watch?id=1&from=chat", plain_text)
        self.assertIn("скачать файл: https://cdn.example/video.mp4", plain_text)

    def test_build_video_link_text_selects_highest_mp4_source(self) -> None:
        html_text, plain_text = _build_max_video_link_text(
            {
                "sources": {
                    "HLS": "https://cdn.example/playlist.m3u8",
                    "MP4_360": "https://cdn.example/360.mp4",
                    "MP4_720": "https://cdn.example/720.mp4",
                }
            }
        )

        self.assertIn("https://cdn.example/720.mp4", html_text)
        self.assertIn("https://cdn.example/720.mp4", plain_text)
        self.assertNotIn("https://cdn.example/360.mp4", html_text)

    def test_append_video_fallback_note_keeps_caption_limit(self) -> None:
        html_text, plain_text = _append_video_fallback_note("caption", "caption")
        self.assertIn("Видео из Max не удалось отправить как файл", html_text)
        self.assertIn("Видео из Max не удалось отправить как файл", plain_text)

    def test_extracts_real_max_reaction(self) -> None:
        self.assertEqual(
            _max_reaction_info_emoji(
                {"counters": [{"count": 1, "reaction": "🔥"}, {"count": 2, "reaction": "❤️"}]}
            ),
            "❤️",
        )
        self.assertIsNone(_max_reaction_info_emoji({"counters": [{"count": 0, "reaction": "👍"}]}))

    def test_detects_viewed_outgoing_max_message(self) -> None:
        msg = {"sender": 24916169, "time": 100}
        chat_info = {"participants": {"24916169": 100, "50530947": 101}}

        self.assertTrue(
            _max_message_viewed_by_other_participant(
                chat_info=chat_info,
                msg=msg,
                own_user_id=24916169,
            )
        )
        self.assertEqual(
            _max_message_state_emoji(
                msg=msg,
                reaction_info={},
                chat_info=chat_info,
                own_user_id=24916169,
            ),
            "👀",
        )

    def test_real_max_reaction_has_priority_over_viewed_state(self) -> None:
        msg = {"sender": 24916169, "time": 100}
        chat_info = {"participants": {"24916169": 100, "50530947": 101}}

        self.assertEqual(
            _max_message_state_emoji(
                msg=msg,
                reaction_info={"counters": [{"count": 1, "reaction": "👍"}]},
                chat_info=chat_info,
                own_user_id=24916169,
            ),
            "👍",
        )


if __name__ == "__main__":
    unittest.main()
