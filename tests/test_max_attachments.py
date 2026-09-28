from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from pymax.types.domain import Message

os.environ.setdefault("TG_BOT_TOKEN", "123456:TEST")
os.environ.setdefault("TG_GROUP_ID", "-1001")

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.methods import SendDocument
from aiogram.types import FSInputFile

import main
import max_bridge
from max_bridge import MaxBridge, _message_to_dict


def _max_file_message() -> Message:
    return Message.model_validate(
        {
            "id": 117347473650886739,
            "chatId": 209286709,
            "sender": 42,
            "text": "",
            "time": 1790000000000,
            "type": "USER",
            "attaches": [
                {
                    "_type": "FILE",
                    "fileId": 777,
                    "name": "Договор.pdf",
                    "size": 1024,
                    "token": "token",
                }
            ],
        }
    )


class TestMaxAttachmentConversion(unittest.TestCase):
    def test_file_attach_keeps_type_after_conversion(self) -> None:
        msg = _message_to_dict(_max_file_message())

        attach = msg["attaches"][0]
        self.assertEqual(attach["_type"], "FILE")
        self.assertEqual(attach["fileId"], 777)
        self.assertEqual(attach["name"], "Договор.pdf")

    def test_file_attach_is_renderable(self) -> None:
        msg = _message_to_dict(_max_file_message())

        attaches = main._get_renderable_max_attaches(msg)

        self.assertEqual([item["_type"] for item in attaches], ["FILE"])

    def test_file_fallback_note_keeps_sender_and_file_name(self) -> None:
        html_text, plain_text = main._append_file_fallback_note(
            "<b>👤 Алексей:</b>",
            "👤 Алексей:",
            "a<b>.pdf",
        )

        self.assertEqual(html_text, "<b>👤 Алексей:</b>\n\n📎 <i>[Файл из Max не удалось скачать: a&lt;b&gt;.pdf]</i>")
        self.assertEqual(plain_text, "👤 Алексей:\n\n📎 [Файл из Max не удалось скачать: a<b>.pdf]")

    def test_file_fallback_note_without_caption(self) -> None:
        html_text, plain_text = main._append_file_fallback_note("", "", None)

        self.assertEqual(html_text, "📎 <i>[Файл из Max не удалось скачать]</i>")
        self.assertEqual(plain_text, "📎 [Файл из Max не удалось скачать]")


class TestMaxFileName(unittest.IsolatedAsyncioTestCase):
    async def test_download_file_keeps_max_name_and_unique_temp_path(self) -> None:
        saved_paths: list[str] = []

        def fake_download(url: str, output):
            saved_paths.append(str(output))
            return output, "application/pdf"

        with patch.object(max_bridge, "download_to_file", side_effect=fake_download):
            bridge = MaxBridge()
            first = await bridge.download_file(
                chat_id=1,
                message_id=10,
                attach_index=0,
                attach={"_type": "FILE", "name": "Счёт.pdf", "url": "https://files.example/a"},
            )
            second = await bridge.download_file(
                chat_id=1,
                message_id=11,
                attach_index=0,
                attach={"_type": "FILE", "name": "Акт.pdf", "url": "https://files.example/b"},
            )

        self.assertEqual(first["name"], "Счёт.pdf")
        self.assertEqual(second["name"], "Акт.pdf")
        self.assertNotEqual(saved_paths[0], saved_paths[1])
        self.assertTrue(saved_paths[0].endswith(".pdf"))

    async def test_telegram_upload_uses_exact_max_file_name(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".pdf") as fh:
            fh.write(b"%PDF-1.4")
            fh.flush()
            session = AiohttpSession()
            try:
                form = session.build_form_data(
                    main.bot,
                    SendDocument(
                        chat_id=-1001,
                        document=FSInputFile(fh.name, filename="Договор №5 (итог).pdf"),
                    ),
                )
                headers = [
                    item[0].headers.get("Content-Disposition", "")
                    for item in form()._parts
                ]
            finally:
                await session.close()

        self.assertIn('filename="Договор №5 (итог).pdf"', " ".join(headers))


class TestMaxFileToTelegram(unittest.IsolatedAsyncioTestCase):
    async def test_max_file_is_sent_as_telegram_document(self) -> None:
        msg = _message_to_dict(_max_file_message())
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
            fh.write(b"%PDF-1.4")
            saved_path = fh.name
        sent = object()

        with (
            patch.object(main, "ensure_user_names_for_ids", AsyncMock()),
            patch.object(main, "resolve_user_name", return_value="Алексей"),
            patch.object(
                main.max_bridge,
                "download_file",
                AsyncMock(return_value={"saved_path": saved_path, "name": "Договор.pdf"}),
            ) as download_file,
            patch.object(main, "_send_tg_document_html_safe", AsyncMock(return_value=sent)) as send_document,
            patch.object(main, "_send_tg_message_html_safe", AsyncMock()) as send_message,
        ):
            result = await main._send_max_message_to_topic(209286709, msg, thread_id=11)

        self.assertEqual(result, [sent])
        download_file.assert_awaited_once_with(
            chat_id=209286709,
            message_id=117347473650886739,
            attach_index=0,
        )
        kwargs = send_document.await_args.kwargs
        self.assertEqual(kwargs["thread_id"], 11)
        self.assertEqual(kwargs["document"].filename, "Договор.pdf")
        self.assertEqual(kwargs["plain_caption"], "👤 Алексей:")
        send_message.assert_not_awaited()
        self.assertFalse(os.path.exists(saved_path))

    async def test_max_file_download_error_says_file_was_lost(self) -> None:
        msg = _message_to_dict(_max_file_message())

        with (
            patch.object(main, "ensure_user_names_for_ids", AsyncMock()),
            patch.object(main, "resolve_user_name", return_value="Алексей"),
            patch.object(main.max_bridge, "download_file", AsyncMock(side_effect=RuntimeError("boom"))),
            patch.object(main, "_send_tg_document_html_safe", AsyncMock()) as send_document,
            patch.object(main, "_send_tg_message_html_safe", AsyncMock(return_value=object())) as send_message,
            self.assertLogs(main.logger, level="ERROR"),
        ):
            await main._send_max_message_to_topic(209286709, msg, thread_id=11)

        send_document.assert_not_awaited()
        send_message.assert_awaited_once()
        self.assertEqual(
            send_message.await_args.kwargs["plain_text"],
            "👤 Алексей:\n\n📎 [Файл из Max не удалось скачать: Договор.pdf]",
        )


if __name__ == "__main__":
    unittest.main()
