from __future__ import annotations

import base64
import unittest

from max_audio import (
    MAX_WAVE_SAMPLES,
    build_audio_attach_payload,
    build_file_attach_payload,
    duration_seconds_to_ms,
    is_attachment_not_ready_error,
    is_connection_error,
    is_invalid_attachment_error,
    normalize_waveform_bytes,
    synthetic_waveform,
    telegram_waveform_to_max_wave,
    voice_mime_type,
    voice_temp_path,
    voice_upload_name,
)


class TestMaxAudioHelpers(unittest.TestCase):
    def test_voice_temp_path_uses_oga_extension(self) -> None:
        self.assertEqual(voice_temp_path("abc123"), "data/t_abc123.oga")

    def test_voice_upload_name_falls_back_to_default(self) -> None:
        self.assertEqual(voice_upload_name(""), "voice.oga")
        self.assertEqual(voice_upload_name("data/t_voice.oga"), "t_voice.oga")

    def test_voice_mime_type_prefers_ogg(self) -> None:
        self.assertEqual(voice_mime_type("voice.oga"), "audio/ogg")
        self.assertEqual(voice_mime_type(None), "audio/ogg")

    def test_duration_seconds_to_ms(self) -> None:
        self.assertEqual(duration_seconds_to_ms(12), 12000)
        self.assertEqual(duration_seconds_to_ms(None), 0)

    def test_normalize_waveform_bytes_downsamples(self) -> None:
        source = bytes(range(128))
        normalized = normalize_waveform_bytes(source, size=64)
        self.assertEqual(len(normalized), 64)
        self.assertEqual(normalized[0], 0)
        self.assertEqual(normalized[-1], 126)

    def test_synthetic_waveform_has_telegram_range(self) -> None:
        wave = synthetic_waveform(15, MAX_WAVE_SAMPLES)
        self.assertEqual(len(wave), MAX_WAVE_SAMPLES)
        self.assertTrue(all(0 <= value <= 31 for value in wave))

    def test_telegram_waveform_to_max_wave_is_base64(self) -> None:
        encoded = telegram_waveform_to_max_wave(bytes([1, 2, 3, 4]), duration_sec=3)
        decoded = base64.b64decode(encoded.encode("ascii"))
        self.assertEqual(len(decoded), MAX_WAVE_SAMPLES)

    def test_build_audio_attach_payload(self) -> None:
        payload = build_audio_attach_payload(
            audio_id=42,
            token="abc",
            duration_ms=5000,
            wave="wave-b64",
        )
        self.assertEqual(payload["_type"], "AUDIO")
        self.assertEqual(payload["audioId"], 42)
        self.assertEqual(payload["token"], "abc")
        self.assertEqual(payload["duration"], 5000)
        self.assertEqual(payload["wave"], "wave-b64")

    def test_build_file_attach_payload(self) -> None:
        payload = build_file_attach_payload(file_id=99)
        self.assertEqual(payload["_type"], "FILE")
        self.assertEqual(payload["fileId"], 99)

    def test_is_attachment_not_ready_error(self) -> None:
        self.assertTrue(is_attachment_not_ready_error("errors.process.attachment.not.ready"))
        self.assertTrue(is_attachment_not_ready_error("errors.process.attachment.video.not.processed"))
        self.assertFalse(is_attachment_not_ready_error("errors.unknown"))

    def test_is_invalid_attachment_error(self) -> None:
        self.assertTrue(is_invalid_attachment_error("Invalid attachment"))
        self.assertFalse(is_invalid_attachment_error("errors.process.attachment.not.ready"))

    def test_is_connection_error(self) -> None:
        self.assertTrue(is_connection_error("Connection closed by the server"))
        self.assertFalse(is_connection_error("Invalid attachment"))


if __name__ == "__main__":
    unittest.main()
