from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
import shutil
import time
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import aiohttp

from config import (
    DATA_DIR,
    MAX_DEVICE_ID,
    MAX_PHONE,
    MAX_SESSION_DIR,
    MAX_SESSION_NAME,
    MAX_TRY_NATIVE_AUDIO_VOICE,
)
from max_audio import (
    build_audio_attach_payload,
    build_file_attach_payload,
    duration_seconds_to_ms,
    is_attachment_not_ready_error,
    is_connection_error,
    is_invalid_attachment_error,
    telegram_waveform_to_max_wave,
    voice_mime_type,
    voice_upload_name,
)
from max_auth import is_max_session_usable


logger = logging.getLogger(__name__)


@dataclass
class MaxMessageEvent:
    chat_id: int | None
    message_id: int | None
    message: dict | None


@dataclass
class MaxDeleteEvent:
    chat_id: int | None
    message_ids: list[int]


@dataclass
class MaxReactionEvent:
    chat_id: int | None
    message_id: int | None
    reaction_info: dict | None


@dataclass
class MaxContactEvent:
    contact: dict


MaxEvent = MaxMessageEvent | MaxDeleteEvent | MaxReactionEvent | MaxContactEvent


def _load_pymax():
    try:
        from pymax import Client, ExtraConfig, File, Photo, Video
    except ImportError as exc:
        raise RuntimeError(
            "maxapi-python is not installed. Run: python -m pip install maxapi-python"
        ) from exc
    return Client, ExtraConfig, File, Photo, Video


def _dump_model(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    if isinstance(value, dict):
        return {_normalize_key(k): _dump_model(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_dump_model(item) for item in value]
    if hasattr(value, "model_dump"):
        try:
            data = value.model_dump(by_alias=True, mode="json")
        except UnicodeDecodeError:
            data = value.model_dump(by_alias=True, mode="python")
        return _dump_model(data)
    return value


def _normalize_key(key: Any) -> Any:
    if not isinstance(key, str):
        return key
    aliases = {
        "chatId": "chatId",
        "sender": "sender",
        "senderId": "senderId",
        "lastMessage": "lastMessage",
        "reactionInfo": "reactionInfo",
        "baseUrl": "baseUrl",
        "photoToken": "photoToken",
        "photoId": "photoId",
        "videoId": "videoId",
        "fileId": "fileId",
        "audioId": "audioId",
        "lottieUrl": "lottieUrl",
        "baseRawIconUrl": "baseRawIconUrl",
        "baseIconUrl": "baseIconUrl",
    }
    return aliases.get(key, key)


def _camel_aliases(value: Any) -> Any:
    if isinstance(value, list):
        return [_camel_aliases(item) for item in value]
    if not isinstance(value, dict):
        return value

    out: dict[Any, Any] = {}
    for key, raw in value.items():
        new_key = _snake_to_camel(key) if isinstance(key, str) else key
        out[new_key] = _camel_aliases(raw)
    if "type" in out and "_type" not in out:
        out["_type"] = str(out["type"]).upper()
    if "sender" in out and "senderId" not in out:
        out["senderId"] = out["sender"]
    if "lastMessage" not in out and "last_message" in value:
        out["lastMessage"] = _camel_aliases(value["last_message"])
    if "reactionInfo" not in out and "reaction_info" in value:
        out["reactionInfo"] = _camel_aliases(value["reaction_info"])
    return out


def _snake_to_camel(key: str) -> str:
    parts = key.split("_")
    if len(parts) == 1:
        return key
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


def _message_to_dict(message: Any) -> dict:
    data = _camel_aliases(_dump_model(message) or {})
    if "id" in data:
        data["id"] = int(data["id"])
    if "chatId" in data and "chat_id" not in data:
        data["chat_id"] = data["chatId"]
    return data


def _chat_to_dict(chat: Any) -> dict:
    data = _camel_aliases(_dump_model(chat) or {})
    if "lastMessage" not in data and isinstance(data.get("last_message"), dict):
        data["lastMessage"] = data["last_message"]
    return data


def _reaction_to_dict(reaction_info: Any) -> dict | None:
    if reaction_info is None:
        return None
    return _camel_aliases(_dump_model(reaction_info) or {})


def suggested_photo_name(chat_id: int, message_id: int, content_type: str | None = None) -> str:
    ext = mimetypes.guess_extension(content_type or "") or ".jpg"
    if ext == ".jpe":
        ext = ".jpg"
    return f"max_{chat_id}_{message_id}{ext}"


def download_to_file(url: str, output: Path | str) -> tuple[Path, str | None]:
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=120) as response:
        content_type = response.headers.get("content-type")
        with output_path.open("wb") as fh:
            shutil.copyfileobj(response, fh)
    return output_path, content_type


class MaxBridge:
    def __init__(self, on_event=None):
        self.on_event = on_event
        self.login_payload: dict[str, Any] | None = None
        self.client: Any | None = None
        self._ready = asyncio.Event()
        self._polling_started = False

    @property
    def session_path(self) -> Path:
        return MAX_SESSION_DIR / MAX_SESSION_NAME

    def is_authorized(self) -> bool:
        return is_max_session_usable()

    def set_on_event(self, on_event) -> None:
        self.on_event = on_event

    def _make_client(self, *, reconnect: bool = True):
        Client, ExtraConfig, *_ = _load_pymax()
        if not MAX_PHONE:
            raise RuntimeError("MAX_PHONE is required for maxapi-python.")
        MAX_SESSION_DIR.mkdir(parents=True, exist_ok=True)
        return Client(
            phone=MAX_PHONE,
            session_name=MAX_SESSION_NAME,
            work_dir=str(MAX_SESSION_DIR),
            extra_config=ExtraConfig(device_id=MAX_DEVICE_ID, reconnect=reconnect),
        )

    def load_sdk(self) -> bool:
        if not self.is_authorized():
            return False
        return True

    async def _on_started(self, client: Any) -> None:
        self.client = client
        self.login_payload = {
            "profile": _camel_aliases(_dump_model(client.me) or {}),
            "contacts": [_camel_aliases(_dump_model(c) or {}) for c in (client.contacts or []) if c],
            "chats": [_chat_to_dict(c) for c in (client.chats or [])],
        }
        self._ready.set()

    async def start_polling(self, chat_ids: list[int] | None = None):
        if self._polling_started:
            return
        self._polling_started = True
        client = self._make_client(reconnect=True)

        @client.on_start()
        async def _started(c):
            await self._on_started(c)

        @client.on_message()
        async def _message(message, c):
            msg = _message_to_dict(message)
            event = MaxMessageEvent(
                chat_id=message.chat_id,
                message_id=int(message.id) if message.id is not None else None,
                message=msg,
            )
            await self._dispatch(event)

        @client.on_message_edit()
        async def _message_edit(message, c):
            msg = _message_to_dict(message)
            event = MaxMessageEvent(
                chat_id=message.chat_id,
                message_id=int(message.id) if message.id is not None else None,
                message=msg,
            )
            await self._dispatch(event)

        @client.on_message_delete()
        async def _message_delete(event, c):
            await self._dispatch(
                MaxDeleteEvent(
                    chat_id=getattr(event, "chat_id", None),
                    message_ids=[int(mid) for mid in (getattr(event, "message_ids", []) or [])],
                )
            )

        @client.on_reaction_update()
        async def _reaction(event, c):
            await self._dispatch(
                MaxReactionEvent(
                    chat_id=getattr(event, "chat_id", None),
                    message_id=int(event.message_id) if getattr(event, "message_id", None) else None,
                    reaction_info={
                        "counters": _camel_aliases(_dump_model(getattr(event, "counters", [])) or []),
                        "totalCount": getattr(event, "total_count", 0),
                    },
                )
            )

        await client.start()

    async def _dispatch(self, event: MaxEvent) -> None:
        if self.on_event is None:
            return
        result = self.on_event(event)
        if asyncio.iscoroutine(result):
            await result

    async def _get_client(self):
        if self.client is None:
            await asyncio.wait_for(self._ready.wait(), timeout=60)
        if self.client is None:
            raise RuntimeError("Max client is not ready")
        return self.client

    async def ensure_chat_subscription(self, chat_id: int) -> bool:
        return True

    async def wait_ready(self, timeout: float = 60) -> None:
        await asyncio.wait_for(self._ready.wait(), timeout=timeout)

    def get_login_chats(self) -> list[dict]:
        payload = self.login_payload if isinstance(self.login_payload, dict) else {}
        chats = payload.get("chats")
        return chats if isinstance(chats, list) else []

    def get_login_contacts(self) -> list[dict]:
        payload = self.login_payload if isinstance(self.login_payload, dict) else {}
        contacts = payload.get("contacts")
        return contacts if isinstance(contacts, list) else []

    def get_own_contact_id(self) -> int | None:
        payload = self.login_payload if isinstance(self.login_payload, dict) else {}
        profile = payload.get("profile") if isinstance(payload, dict) else None
        if not isinstance(profile, dict):
            return None
        value = profile.get("id") or profile.get("contactId") or profile.get("userId")
        return int(value) if value is not None else None

    async def get_contacts_by_ids(self, contact_ids: list[int]) -> list[dict]:
        client = await self._get_client()
        contacts = await client.get_users([int(cid) for cid in contact_ids])
        return [_camel_aliases(_dump_model(contact) or {}) for contact in contacts if contact]

    async def list_chats(self) -> list[dict]:
        client = await self._get_client()
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                chats = await client.fetch_chats()
                return [_chat_to_dict(chat) for chat in (chats or [])]
            except UnicodeDecodeError as exc:
                last_error = exc
                await asyncio.sleep(1 + attempt)
        if last_error is not None:
            return await self._list_chats_with_oneshot_client()
        return []

    async def _list_chats_with_oneshot_client(self) -> list[dict]:
        client = self._make_client(reconnect=False)
        result: list[dict] = []
        completed = False

        @client.on_start()
        async def _started(c):
            nonlocal result, completed
            chats = await c.fetch_chats()
            result = [_chat_to_dict(chat) for chat in (chats or [])]
            completed = True
            await c.stop()

        try:
            await client.start()
        except (asyncio.CancelledError, Exception):
            if not completed:
                raise
        return result

    async def get_chat_info(self, chat_id: int):
        client = await self._get_client()
        return _chat_to_dict(await client.get_chat(int(chat_id)))

    async def get_message(self, chat_id: int, message_id: int):
        client = await self._get_client()
        message = await client.get_message(int(chat_id), int(message_id))
        return _message_to_dict(message) if message else None

    async def get_last_message(self, chat_id: int):
        history = await self.get_chat_history(chat_id=chat_id, count=1)
        messages = history.get("messages", [])
        return messages[0] if messages else None

    async def get_chat_history(self, **kwargs):
        client = await self._get_client()
        chat_id = int(kwargs["chat_id"])
        count = int(kwargs.get("count") or kwargs.get("backward") or 40)
        from_time = kwargs.get("from_time")
        messages = await client.fetch_history(
            chat_id=chat_id,
            backward=count,
            forward=0,
            from_time=from_time,
            get_chat=bool(kwargs.get("get_chat", False)),
            get_messages=bool(kwargs.get("get_messages", True)),
        )
        converted = [_message_to_dict(message) for message in (messages or [])]
        converted.sort(key=lambda item: int(item.get("time") or 0), reverse=True)
        return {"messages": converted}

    async def get_reactions(self, chat_id: int, message_ids: list[int]):
        return {"messagesReactions": {}}

    async def send_text(self, chat_id: int, text: str, **kwargs):
        client = await self._get_client()
        reply_to = kwargs.get("reply_to")
        message = await client.send_message(int(chat_id), text or "", reply_to=reply_to)
        return _message_to_dict(message) if message else None

    async def send_local_photo(self, **kwargs):
        return await self._send_local_attachment("photo", **kwargs)

    async def send_local_video(self, **kwargs):
        return await self._send_local_attachment("video", **kwargs)

    async def send_local_file(self, **kwargs):
        return await self._send_local_attachment("file", **kwargs)

    async def send_local_audio(self, **kwargs):
        path = kwargs.get("path") or kwargs.get("file_path") or kwargs.get("input_path")
        if not path:
            raise ValueError("path is required")
        chat_id = int(kwargs["chat_id"])
        caption = kwargs.get("text") or kwargs.get("caption") or ""
        reply_to = kwargs.get("reply_to")
        duration_sec = int(kwargs.get("duration") or 0)
        wave = kwargs.get("wave") or kwargs.get("waveform")
        if wave is None:
            if kwargs.get("telegram_waveform") is not None:
                wave = telegram_waveform_to_max_wave(
                    kwargs.get("telegram_waveform"),
                    duration_sec=duration_sec,
                )
            else:
                wave = telegram_waveform_to_max_wave(None, duration_sec=duration_sec)

        if MAX_TRY_NATIVE_AUDIO_VOICE:
            try:
                native_message = await self._try_send_native_audio_voice(
                    chat_id=chat_id,
                    input_path=Path(path),
                    caption=caption,
                    reply_to=reply_to,
                    duration_sec=duration_sec,
                    wave=wave,
                )
                if native_message is not None:
                    return native_message
            except Exception as exc:
                if not is_invalid_attachment_error(str(exc)) and not is_connection_error(str(exc)):
                    raise
                logger.warning("TG->Max: Native AUDIO voice rejected by Max: %s", exc)
            await self._pause_for_reconnect(seconds=3.0)

        upload_info = await self._upload_voice_file_with_retry(
            chat_id=chat_id,
            input_path=Path(path),
            prep_type="FILE",
        )
        return await self._send_voice_file_attach(
            chat_id=chat_id,
            caption=caption,
            reply_to=reply_to,
            file_id=int(upload_info["file_id"]),
        )

    async def _send_voice_file_attach(
        self,
        *,
        chat_id: int,
        caption: str,
        reply_to: int | None,
        file_id: int,
        max_attempts: int = 4,
    ):
        attach = build_file_attach_payload(file_id=file_id)
        last_error: Exception | None = None
        for attempt in range(max_attempts):
            try:
                client = await self._get_client()
                logger.info(
                    "TG->Max: Sending voice as FILE attach chat_id=%s file_id=%s attempt=%s payload=%s",
                    chat_id,
                    file_id,
                    attempt + 1,
                    attach,
                )
                message = await self._send_message_with_attach(
                    client,
                    chat_id=chat_id,
                    text=caption,
                    attach=attach,
                    reply_to=reply_to,
                )
                return _message_to_dict(message) if message else None
            except Exception as exc:
                last_error = exc
                error_text = str(exc)
                if attempt + 1 >= max_attempts:
                    raise
                if (
                    is_connection_error(error_text)
                    or is_attachment_not_ready_error(error_text)
                    or "upload" in error_text.lower()
                ):
                    logger.warning(
                        "TG->Max: FILE voice send failed chat_id=%s attempt=%s: %s",
                        chat_id,
                        attempt + 1,
                        exc,
                    )
                    await self._pause_for_reconnect(seconds=2.5 + attempt)
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise RuntimeError("Failed to send voice FILE attachment to Max")

    async def _pause_for_reconnect(self, seconds: float = 2.5) -> None:
        await asyncio.sleep(seconds)
        try:
            await self.wait_ready(timeout=30)
        except TimeoutError:
            logger.warning("TG->Max: Max client not ready after reconnect pause")

    async def _try_send_native_audio_voice(
        self,
        *,
        chat_id: int,
        input_path: Path,
        caption: str,
        reply_to: int | None,
        duration_sec: int,
        wave: str,
    ):
        client = await self._get_client()
        duration_ms = duration_seconds_to_ms(duration_sec)
        upload_info = await self._upload_voice_file_with_retry(
            chat_id=chat_id,
            input_path=input_path,
            prep_type="AUDIO",
        )
        attach = build_audio_attach_payload(
            audio_id=upload_info["file_id"],
            token=upload_info.get("token"),
            duration_ms=duration_ms,
            wave=wave,
        )
        logger.info(
            "TG->Max: Trying native AUDIO voice chat_id=%s payload=%s",
            chat_id,
            attach,
        )
        message = await self._send_message_with_attach(
            client,
            chat_id=chat_id,
            text=caption,
            attach=attach,
            reply_to=reply_to,
        )
        return _message_to_dict(message) if message else None

    async def _upload_voice_file_with_retry(
        self,
        *,
        chat_id: int,
        input_path: Path,
        prep_type: str,
        max_attempts: int = 4,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(max_attempts):
            try:
                active_client = await self._get_client()
                return await self._upload_voice_file(
                    active_client,
                    chat_id=chat_id,
                    input_path=input_path,
                    prep_type=prep_type,
                )
            except Exception as exc:
                last_error = exc
                error_text = str(exc)
                if attempt + 1 >= max_attempts:
                    raise
                if is_connection_error(error_text) or "upload" in error_text.lower():
                    logger.warning(
                        "TG->Max: Voice upload failed chat_id=%s prep_type=%s attempt=%s: %s",
                        chat_id,
                        prep_type,
                        attempt + 1,
                        exc,
                    )
                    await self._pause_for_reconnect(seconds=2.5 + attempt)
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise RuntimeError("Failed to upload voice file to Max")

    async def _upload_voice_file(
        self,
        client: Any,
        *,
        chat_id: int,
        input_path: Path,
        prep_type: str,
    ) -> dict[str, Any]:
        from pymax.api.uploads.models import FileUploadResponse
        from pymax.api.uploads.payloads import UploadPayload
        from pymax.exceptions import UploadError
        from pymax.protocol import Opcode
        from pymax.types.events import FileUploadSignal

        app = client._app
        uploads = app.api.uploads
        file_path = Path(input_path)
        if not file_path.is_file():
            raise FileNotFoundError(f"Audio file not found: {file_path}")

        data = await app.invoke(
            Opcode.FILE_UPLOAD,
            UploadPayload().model_dump(),
        )
        try:
            response = FileUploadResponse.model_validate(data.payload)
            upload_info = response.info[0]
        except (IndexError, TypeError, ValueError) as exc:
            raise UploadError("Invalid audio upload response from Max") from exc

        file_id = upload_info.file_id
        token = upload_info.token
        filename = voice_upload_name(file_path)
        mime_type = voice_mime_type(file_path)

        await app.invoke(
            Opcode.MSG_TYPING,
            {"chatId": chat_id, "type": prep_type},
        )

        loop = asyncio.get_running_loop()
        future: asyncio.Future[FileUploadSignal] = loop.create_future()
        uploads.file_upload_waiters[file_id] = future

        try:
            async with aiohttp.ClientSession(proxy=app.config.proxy) as session:
                await self._post_multipart_upload(
                    session,
                    url=upload_info.url,
                    file_path=file_path,
                    filename=filename,
                    mime_type=mime_type,
                )
                await asyncio.wait_for(future, 60)
        except asyncio.TimeoutError as exc:
            raise UploadError(f"Timed out waiting for audio processing file_id={file_id}") from exc
        finally:
            uploads.file_upload_waiters.pop(file_id, None)

        logger.info(
            "TG->Max: Voice file uploaded chat_id=%s prep_type=%s file_id=%s token_present=%s",
            chat_id,
            prep_type,
            file_id,
            bool(token),
        )
        return {"file_id": file_id, "token": token}

    async def _post_multipart_upload(
        self,
        session: aiohttp.ClientSession,
        *,
        url: str,
        file_path: Path,
        filename: str,
        mime_type: str,
    ) -> None:
        from pymax.exceptions import UploadError

        form = aiohttp.FormData()
        form.add_field(
            "file",
            file_path.read_bytes(),
            filename=filename,
            content_type=mime_type,
        )
        headers = {
            "Accept": "*/*",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Origin": "https://web.max.ru",
            "Referer": "https://web.max.ru/",
        }
        async with session.post(url, data=form, headers=headers) as http_response:
            if http_response.status != HTTPStatus.OK:
                body = await http_response.text()
                raise UploadError(
                    f"Audio upload failed with status {http_response.status}: {body[:300]}"
                )

    async def _send_message_with_attach(
        self,
        client: Any,
        *,
        chat_id: int,
        text: str,
        attach: dict,
        reply_to: int | None = None,
        max_attempts: int = 8,
    ):
        from pymax.api.binding import bind_api_model
        from pymax.api.response import require_payload_model
        from pymax.exceptions import ApiError
        from pymax.protocol import Opcode
        from pymax.types.domain import Message

        app = client._app
        last_error: Exception | None = None
        for attempt in range(max_attempts):
            cid = int(time.time() * 1000) + attempt
            payload: dict[str, Any] = {
                "chatId": chat_id,
                "message": {
                    "text": text or "",
                    "cid": cid,
                    "elements": [],
                    "attaches": [attach],
                },
                "notify": True,
            }
            if reply_to is not None:
                payload["message"]["link"] = {
                    "type": "REPLY",
                    "messageId": int(reply_to),
                }
            try:
                response = await app.invoke(Opcode.MSG_SEND, payload)
                return bind_api_model(app, require_payload_model(response, Message))
            except ApiError as exc:
                last_error = exc
                error_text = " ".join(
                    part
                    for part in (exc.error, exc.message, exc.localized_message, str(exc))
                    if part
                )
                if is_attachment_not_ready_error(error_text) and attempt + 1 < max_attempts:
                    await asyncio.sleep(1 + attempt)
                    continue
                raise
            except (ConnectionError, OSError) as exc:
                last_error = exc
                if attempt + 1 < max_attempts:
                    await self._pause_for_reconnect()
                    client = await self._get_client()
                    app = client._app
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise RuntimeError("Failed to send attachment to Max")

    async def _send_local_attachment(self, kind: str, **kwargs):
        client = await self._get_client()
        _, _, File, Photo, Video = _load_pymax()
        path = kwargs.get("path") or kwargs.get("file_path") or kwargs.get("input_path")
        if not path:
            raise ValueError("path is required")
        caption = kwargs.get("text") or kwargs.get("caption") or ""
        chat_id = int(kwargs["chat_id"])
        reply_to = kwargs.get("reply_to")
        cls = {"photo": Photo, "video": Video, "file": File}[kind]
        message = await client.send_message(
            chat_id=chat_id,
            text=caption,
            reply_to=reply_to,
            attachments=[cls(path=str(path))],
        )
        return _message_to_dict(message) if message else None

    async def edit_message(self, chat_id: int, message_id: int, text: str, **kwargs):
        client = await self._get_client()
        message = await client.edit_message(int(chat_id), int(message_id), text or "")
        return _message_to_dict(message) if message else None

    async def download_photo(self, **kwargs):
        attach = await self._select_attachment(kwargs, "PHOTO")
        url = attach.get("baseUrl") or attach.get("url")
        if not url:
            raise RuntimeError("PHOTO attachment does not contain a download URL")
        return await self._download_url(url, kwargs, default_ext=".jpg")

    async def resolve_video_urls(self, **kwargs):
        attach = await self._select_attachment(kwargs, "VIDEO")
        url = attach.get("url")
        if not url:
            client = await self._get_client()
            video_id = attach.get("videoId")
            if video_id is not None:
                data = await client.get_video_by_id(
                    int(kwargs["chat_id"]),
                    int(kwargs["message_id"]),
                    int(video_id),
                )
                video_data = _camel_aliases(_dump_model(data) or {})
                url = video_data.get("url")
        return {"selected_url": url, "external_url": url, "sources": {"MP4": url} if url else {}}

    async def download_video(self, **kwargs):
        urls = await self.resolve_video_urls(**kwargs)
        url = urls.get("selected_url")
        if not url:
            raise RuntimeError("VIDEO attachment does not contain a download URL")
        result = await self._download_url(url, kwargs, default_ext=".mp4")
        result.update(urls)
        return result

    async def download_audio(self, **kwargs):
        attach = await self._select_attachment(kwargs, "AUDIO")
        url = attach.get("url")
        if not url:
            raise RuntimeError("AUDIO attachment does not contain a download URL")
        return await self._download_url(url, kwargs, default_ext=".mp3")

    async def download_file(self, **kwargs):
        attach = await self._select_attachment(kwargs, "FILE")
        url = attach.get("url")
        if not url:
            client = await self._get_client()
            file_id = attach.get("fileId")
            if file_id is not None:
                data = await client.get_file_by_id(
                    int(kwargs["chat_id"]),
                    int(kwargs["message_id"]),
                    int(file_id),
                )
                file_data = _camel_aliases(_dump_model(data) or {})
                url = file_data.get("url")
        if not url:
            raise RuntimeError("FILE attachment does not contain a download URL")
        result = await self._download_url(url, kwargs, default_name=attach.get("name"))
        result["name"] = attach.get("name") or Path(result["saved_path"]).name
        return result

    async def download_sticker(self, **kwargs):
        attach = await self._select_attachment(kwargs, "STICKER")
        result: dict[str, str] = {}
        if attach.get("url"):
            preview = await self._download_url(attach["url"], kwargs, default_ext=".webp")
            result["preview_path"] = preview["saved_path"]
        if attach.get("lottieUrl"):
            lottie = await self._download_url(attach["lottieUrl"], kwargs, default_ext=".json")
            result["lottie_json_path"] = lottie["saved_path"]
        if not result:
            raise RuntimeError("STICKER attachment does not contain a download URL")
        return result

    async def _select_attachment(self, kwargs: dict, expected_type: str) -> dict:
        attach = kwargs.get("attach")
        if isinstance(attach, dict):
            return attach
        message = await self.get_message(int(kwargs["chat_id"]), int(kwargs["message_id"]))
        attaches = message.get("attaches", []) if isinstance(message, dict) else []
        candidates = [
            item
            for item in attaches
            if isinstance(item, dict) and self._attachment_matches_type(item, expected_type)
        ]
        index = int(kwargs.get("attach_index") or 0)
        if index >= len(candidates):
            raise IndexError(f"{expected_type} attachment index {index} is out of range")
        return candidates[index]

    @staticmethod
    def _attachment_matches_type(item: dict, expected_type: str) -> bool:
        attach_type = item.get("_type") or item.get("type")
        if attach_type == expected_type:
            return True
        if expected_type == "PHOTO" and "photoToken" in item:
            return True
        return False

    async def _download_url(
        self,
        url: str,
        kwargs: dict,
        *,
        default_ext: str = "",
        default_name: str | None = None,
    ) -> dict:
        path = self._media_path(url, kwargs, default_ext=default_ext, default_name=default_name)
        saved_path, content_type = await asyncio.to_thread(download_to_file, url, path)
        return {
            "saved_path": str(saved_path),
            "content_type": content_type,
            "external_url": url,
        }

    def _media_path(
        self,
        url: str,
        kwargs: dict,
        *,
        default_ext: str = "",
        default_name: str | None = None,
    ) -> Path:
        chat_id = kwargs.get("chat_id", "chat")
        message_id = kwargs.get("message_id", "message")
        attach_index = kwargs.get("attach_index", 0)
        url_name = Path(urlparse(url).path).name
        name = default_name or url_name
        if not name:
            name = f"max_{chat_id}_{message_id}_{attach_index}{default_ext}"
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
        if "." not in name and default_ext:
            name += default_ext
        return DATA_DIR / name
