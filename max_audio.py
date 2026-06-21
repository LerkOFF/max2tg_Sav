from __future__ import annotations

import base64
import math
import mimetypes
from pathlib import Path

MAX_WAVE_SAMPLES = 64
VOICE_EXTENSION = ".oga"
VOICE_MIME = "audio/ogg"


def voice_temp_path(file_id: str) -> str:
    return f"data/t_{file_id}{VOICE_EXTENSION}"


def voice_upload_name(path: str | Path) -> str:
    name = Path(path).name.strip()
    if name:
        return name
    return f"voice{VOICE_EXTENSION}"


def voice_mime_type(path: str | Path | None = None) -> str:
    if path is not None:
        guessed, _ = mimetypes.guess_type(str(path))
        if guessed and guessed.startswith("audio/"):
            return guessed
    return VOICE_MIME


def duration_seconds_to_ms(duration_sec: int | None) -> int:
    if not duration_sec or duration_sec < 0:
        return 0
    return int(duration_sec) * 1000


def normalize_waveform_bytes(data: bytes, size: int = MAX_WAVE_SAMPLES) -> bytes:
    if not data:
        return synthetic_waveform(0, size)
    if len(data) == size:
        return data
    if len(data) > size:
        step = len(data) / size
        return bytes(data[int(i * step)] for i in range(size))
    out = bytearray(size)
    for i in range(size):
        src_idx = int(i * len(data) / size)
        out[i] = data[min(src_idx, len(data) - 1)]
    return bytes(out)


def synthetic_waveform(duration_sec: int, size: int = MAX_WAVE_SAMPLES) -> bytes:
    out = bytearray(size)
    seed = max(1, min(int(duration_sec or 0), 120))
    for i in range(size):
        t = i / max(size - 1, 1)
        amp = int(18 + 8 * math.sin(t * math.pi * 3) + (seed % 5))
        out[i] = max(0, min(31, amp))
    return bytes(out)


def telegram_waveform_to_max_wave(
    waveform: bytes | None,
    *,
    duration_sec: int = 0,
    size: int = MAX_WAVE_SAMPLES,
) -> str:
    samples = normalize_waveform_bytes(waveform or b"", size) if waveform else synthetic_waveform(duration_sec, size)
    return base64.b64encode(samples).decode("ascii")


def build_audio_attach_payload(
    *,
    audio_id: int,
    token: str | None,
    duration_ms: int,
    wave: str | None = None,
) -> dict:
    attach: dict = {
        "_type": "AUDIO",
        "audioId": int(audio_id),
        "duration": int(duration_ms),
    }
    if token:
        attach["token"] = token
    if wave:
        attach["wave"] = wave
    return attach


def is_attachment_not_ready_error(error_text: str) -> bool:
    lowered = error_text.lower()
    return "not.ready" in lowered or "not.processed" in lowered
