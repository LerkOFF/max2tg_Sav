from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from config import DATA_DIR, MAX_DEVICE_ID, MAX_PHONE, MAX_SESSION_DIR, MAX_SESSION_NAME


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
    if isinstance(value, dict):
        return {_normalize_key(k): _dump_model(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_dump_model(item) for item in value]
    if hasattr(value, "model_dump"):
        data = value.model_dump(by_alias=True, mode="json")
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
        return self.session_path.exists()

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
        chats = await client.fetch_chats()
        return [_chat_to_dict(chat) for chat in (chats or [])]

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
        client = await self._get_client()
        raw = await client.get_reactions(int(chat_id), [str(mid) for mid in message_ids])
        return {
            "messagesReactions": {
                str(mid): _reaction_to_dict(info)
                for mid, info in (raw or {}).items()
            }
        }

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
            if isinstance(item, dict) and (item.get("_type") == expected_type or item.get("type") == expected_type)
        ]
        index = int(kwargs.get("attach_index") or 0)
        if index >= len(candidates):
            raise IndexError(f"{expected_type} attachment index {index} is out of range")
        return candidates[index]

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
