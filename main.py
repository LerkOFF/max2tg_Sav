from __future__ import annotations
import asyncio, json, logging, re, os, socket
import faulthandler
import html
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path
import time
import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import Message, FSInputFile, ReactionTypeEmoji
from config import CHAT_RECONCILE_INTERVAL_SECONDS, TG_BOT_TOKEN, TG_GROUP_ID, TG_POLLING_TIMEOUT
from database import BridgeDB
from max_audio import voice_temp_path
from max_bridge import (
    MaxBridge,
    MaxContactEvent,
    MaxDeleteEvent,
    MaxEvent,
    MaxMessageEvent,
    MaxReactionEvent,
    download_to_file,
    suggested_photo_name,
)

LOG_PATH = Path(os.getenv("BRIDGE_LOG_PATH", "logs/bridge.log"))
FAULT_LOG_PATH = Path(os.getenv("BRIDGE_FAULT_LOG_PATH", "logs/faulthandler.log"))
LOG_MAX_BYTES = int(os.getenv("BRIDGE_LOG_MAX_BYTES", str(5 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("BRIDGE_LOG_BACKUP_COUNT", "5"))
_fault_log_file = None


def configure_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    FAULT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s:%(name)s:%(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)

    global _fault_log_file
    _fault_log_file = FAULT_LOG_PATH.open("a", encoding="utf-8")
    faulthandler.enable(file=_fault_log_file, all_threads=True)


configure_logging()
logger = logging.getLogger(__name__)

def build_telegram_session() -> AiohttpSession:
    session = AiohttpSession(
        limit=20,
        timeout=75.0,
    )
    session._connector_init.update(
        {
            "family": socket.AF_INET,
            "enable_cleanup_closed": True,
            "keepalive_timeout": 75,
        }
    )
    return session


bot = Bot(token=TG_BOT_TOKEN, session=build_telegram_session())
dp = Dispatcher()
db = BridgeDB()
max_bridge = MaxBridge()
_chat_topic_locks: dict[int, asyncio.Lock] = {}
PID_FILE = Path("data/bot.pid")
_recent_bridge_message_ids: dict[int, float] = {}
_recent_tg_reaction_message_keys: dict[tuple[int, int], float] = {}
_known_tg_message_reactions: dict[tuple[int, int], str] = {}
_invalid_tg_reaction_emojis: set[str] = set()
_tg_reaction_lock = asyncio.Lock()
_last_tg_reaction_at = 0.0
BRIDGE_MESSAGE_TTL_SECONDS = 300.0
BACKFILL_MESSAGES_PER_CHAT = int(os.getenv("BACKFILL_MESSAGES_PER_CHAT", "10"))
BACKFILL_PAGE_SIZE = int(os.getenv("BACKFILL_PAGE_SIZE", "50"))
STARTUP_BACKFILL_ENABLED = os.getenv("STARTUP_BACKFILL_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
STARTUP_BACKFILL_CHATS_LIMIT = int(os.getenv("STARTUP_BACKFILL_CHATS_LIMIT", "10"))
STARTUP_BACKFILL_MESSAGES_PER_CHAT = int(os.getenv("STARTUP_BACKFILL_MESSAGES_PER_CHAT", "10"))
MAX_STATE_SYNC_MESSAGES_PER_CHAT = int(os.getenv("MAX_STATE_SYNC_MESSAGES_PER_CHAT", "20"))
MAX_STATE_SYNC_INTERVAL_SECONDS = int(os.getenv("MAX_STATE_SYNC_INTERVAL_SECONDS", "300"))
MAX_STARTUP_RETRY_SECONDS = int(os.getenv("MAX_STARTUP_RETRY_SECONDS", "30"))
TG_SEND_NETWORK_RETRY_ATTEMPTS = int(os.getenv("TG_SEND_NETWORK_RETRY_ATTEMPTS", "3"))
TG_SEND_NETWORK_RETRY_DELAY_SECONDS = float(os.getenv("TG_SEND_NETWORK_RETRY_DELAY_SECONDS", "2.0"))
TG_VIDEO_SEND_TIMEOUT_SECONDS = int(os.getenv("TG_VIDEO_SEND_TIMEOUT_SECONDS", "180"))
TG_REACTION_MIN_INTERVAL_SECONDS = float(os.getenv("TG_REACTION_MIN_INTERVAL_SECONDS", "0.5"))
TG_REACTION_UPDATE_TTL_SECONDS = float(os.getenv("TG_REACTION_UPDATE_TTL_SECONDS", "3600"))
TG_VIEWED_REACTION = os.getenv("TG_VIEWED_REACTION", "👀")
TG_FORUM_TOPIC_CREATE_DELAY_SECONDS = float(os.getenv("TG_FORUM_TOPIC_CREATE_DELAY_SECONDS", "1.0"))


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_looks_like_this_bot(pid: int) -> bool:
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    normalized = cmdline.replace("\x00", " ")
    return "python" in normalized.lower() and "main.py" in normalized


def ensure_single_instance() -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    current_pid = os.getpid()
    if PID_FILE.exists():
        raw_pid = PID_FILE.read_text(encoding="utf-8").strip()
        try:
            existing_pid = int(raw_pid)
        except ValueError:
            existing_pid = 0
        if (
            existing_pid
            and existing_pid != current_pid
            and _pid_is_running(existing_pid)
            and _pid_looks_like_this_bot(existing_pid)
        ):
            raise RuntimeError(f"Bot is already running with PID {existing_pid}")
        PID_FILE.unlink(missing_ok=True)
    PID_FILE.write_text(f"{current_pid}\n", encoding="utf-8")


def cleanup_pid_file() -> None:
    if not PID_FILE.exists():
        return
    try:
        raw_pid = PID_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if raw_pid == str(os.getpid()):
        PID_FILE.unlink(missing_ok=True)


def remember_bridge_message(message_id: int) -> None:
    now = time.monotonic()
    expired = [mid for mid, ts in _recent_bridge_message_ids.items() if now - ts > BRIDGE_MESSAGE_TTL_SECONDS]
    for mid in expired:
        _recent_bridge_message_ids.pop(mid, None)
    _recent_bridge_message_ids[message_id] = now


def is_recent_bridge_message(message_id: int | None) -> bool:
    if not message_id:
        return False
    ts = _recent_bridge_message_ids.get(int(message_id))
    if ts is None:
        return False
    if time.monotonic() - ts > BRIDGE_MESSAGE_TTL_SECONDS:
        _recent_bridge_message_ids.pop(int(message_id), None)
        return False
    return True


def remember_tg_reaction_update(chat_id: int, message_id: int) -> None:
    now = time.monotonic()
    expired = [
        key
        for key, ts in _recent_tg_reaction_message_keys.items()
        if now - ts > TG_REACTION_UPDATE_TTL_SECONDS
    ]
    for key in expired:
        _recent_tg_reaction_message_keys.pop(key, None)
    _recent_tg_reaction_message_keys[(chat_id, message_id)] = now


def is_recent_tg_reaction_update(chat_id: int, message_id: int) -> bool:
    key = (chat_id, message_id)
    ts = _recent_tg_reaction_message_keys.get(key)
    if ts is None:
        return False
    if time.monotonic() - ts > TG_REACTION_UPDATE_TTL_SECONDS:
        _recent_tg_reaction_message_keys.pop(key, None)
        return False
    return True


def _chat_last_activity(chat: dict | None) -> int:
    if not isinstance(chat, dict):
        return 0
    last_message = chat.get("lastMessage")
    if not isinstance(last_message, dict):
        return 0
    try:
        return int(last_message.get("time") or 0)
    except (TypeError, ValueError):
        return 0


def _build_chat_activity_index(chats: list[dict]) -> dict[int, int]:
    activity: dict[int, int] = {}
    for chat in chats:
        if not isinstance(chat, dict):
            continue
        chat_id = chat.get("id")
        if chat_id is None:
            continue
        try:
            activity[int(chat_id)] = _chat_last_activity(chat)
        except (TypeError, ValueError):
            continue
    return activity


def get_chat_title(chat: dict, chat_id: int) -> str:
    title = chat.get("title")
    if title:
        return title
    if chat.get("type") == "DIALOG":
        title = _resolve_dialog_title_from_participants(chat)
        if title:
            return title
        title = clean_title(chat.get("lastMessage", {}).get("text", ""))
        if title:
            return title
    return f"Чат {chat_id}"


def clean_title(text: str) -> str:
    if not text: return ""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines: return ""
    skip = ["добро пожаловать", "привет", "здравствуйте", "welcome", "на связи", "это бот", "помощь"]
    cand = lines[0]
    for l in lines:
        if not any(k in l.lower() for k in skip):
            cand = l
            break
    m = re.search(r'[a-zA-Zа-яА-Я0-9]', cand)
    res = cand[m.start():].strip() if m else cand.strip()
    return res.split(". ")[0].split("\n")[0][:50].strip(".,!?- ")

def format_max_text(text: str, elements: list) -> str:
    if not elements or not text:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    try:
        utf16 = text.encode("utf-16-le")
    except Exception:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    inserts = []
    for i, e in enumerate(elements):
        frm, lng, typ = e.get("from") or 0, e.get("length") or 0, e.get("type")
        tag_open, tag_close = "", ""
        if typ == "STRONG": tag_open, tag_close = "<b>", "</b>"
        elif typ == "ITALIC": tag_open, tag_close = "<i>", "</i>"
        elif typ == "LINK":
            url = e.get("attributes", {}).get("url", "")
            if url:
                url_esc = url.replace("&", "&amp;").replace('"', "&quot;")
                tag_open, tag_close = f'<a href="{url_esc}">', "</a>"
        elif typ in ("CODE", "PRE"): tag_open, tag_close = "<code>", "</code>"
        elif typ == "STRIKETHROUGH": tag_open, tag_close = "<s>", "</s>"
        elif typ == "UNDERLINE": tag_open, tag_close = "<u>", "</u>"
        
        if tag_open:
            inserts.append({"offset": frm, "tag": tag_open, "is_close": False, "len": lng, "id": i})
            inserts.append({"offset": frm + lng, "tag": tag_close, "is_close": True, "len": lng, "id": i})
            
    inserts.sort(key=lambda x: (x["offset"], x["is_close"], -x["len"] if not x["is_close"] else x["len"], -x["id"] if x["is_close"] else x["id"]))
    
    result, last_offset = "", 0
    for ins in inserts:
        off = ins["offset"]
        if off > last_offset:
            part = utf16[last_offset*2 : off*2].decode("utf-16-le", errors="ignore")
            result += part.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            last_offset = off
        result += ins["tag"]
        
    if last_offset * 2 < len(utf16):
        part = utf16[last_offset*2:].decode("utf-16-le", errors="ignore")
        result += part.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        
    return result

@lru_cache(maxsize=1)
def load_user_names() -> dict:
    try:
        with open("data/user_names.json", "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _format_contact_name(contact: dict) -> str:
    names = contact.get("names") if isinstance(contact, dict) else None
    if not isinstance(names, list):
        return ""

    for preferred_type in ("CUSTOM", "ONEME"):
        for item in names:
            if not isinstance(item, dict) or item.get("type") != preferred_type:
                continue
            first_name = str(item.get("firstName") or "").strip()
            last_name = str(item.get("lastName") or "").strip()
            full_name = " ".join(part for part in [first_name, last_name] if part).strip()
            if full_name:
                return full_name
            display_name = str(item.get("name") or "").strip()
            if display_name:
                return display_name

    for item in names:
        if not isinstance(item, dict):
            continue
        display_name = str(item.get("name") or "").strip()
        if display_name:
            return display_name
    return ""


def refresh_user_names_from_login_payload() -> None:
    names = load_user_names().copy()
    login_payload = max_bridge.login_payload if isinstance(max_bridge.login_payload, dict) else {}
    profile = login_payload.get("profile") if isinstance(login_payload, dict) else None
    own_contact = profile.get("contact") if isinstance(profile, dict) else None
    if isinstance(own_contact, dict) and own_contact.get("id") is not None:
        own_name = _format_contact_name(own_contact)
        if own_name:
            names[str(own_contact["id"])] = own_name
        else:
            names.setdefault(str(own_contact["id"]), str(own_contact["id"]))

    for contact in max_bridge.get_login_contacts():
        if not isinstance(contact, dict):
            continue
        contact_id = contact.get("id")
        if contact_id is None:
            continue
        display_name = _format_contact_name(contact)
        if display_name:
            names[str(contact_id)] = display_name

    user_names_path = Path("data/user_names.json")
    user_names_path.parent.mkdir(parents=True, exist_ok=True)
    next_text = json.dumps(names, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    current_text = user_names_path.read_text(encoding="utf-8") if user_names_path.exists() else ""
    if next_text != current_text:
        user_names_path.write_text(next_text, encoding="utf-8")
        load_user_names.cache_clear()
        logger.info("Updated user name cache with %s Max contacts", len(names))


def _save_user_names(names: dict) -> None:
    user_names_path = Path("data/user_names.json")
    user_names_path.parent.mkdir(parents=True, exist_ok=True)
    next_text = json.dumps(names, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    current_text = user_names_path.read_text(encoding="utf-8") if user_names_path.exists() else ""
    if next_text != current_text:
        user_names_path.write_text(next_text, encoding="utf-8")
        load_user_names.cache_clear()


async def ensure_user_names_for_ids(contact_ids: list[int]) -> None:
    names = load_user_names().copy()
    missing_ids = sorted(
        {
            int(contact_id)
            for contact_id in contact_ids
            if contact_id is not None and str(int(contact_id)) not in names
        }
    )
    if not missing_ids:
        return
    try:
        contacts = await max_bridge.get_contacts_by_ids(missing_ids)
    except Exception as exc:
        logger.warning("Failed to load Max contact names for ids=%s: %s", missing_ids, exc)
        return

    changed = False
    for contact in contacts:
        if not isinstance(contact, dict) or contact.get("id") is None:
            continue
        display_name = _format_contact_name(contact)
        if not display_name:
            continue
        names[str(contact["id"])] = display_name
        changed = True
    if changed:
        _save_user_names(names)
        logger.info("Resolved %s Max contact names", len(contacts))


async def ensure_user_names_for_chat(chat: dict) -> None:
    contact_ids: set[int] = set()
    participants = chat.get("participants") if isinstance(chat, dict) else None
    if isinstance(participants, dict):
        for participant_id in participants.keys():
            try:
                contact_ids.add(int(participant_id))
            except (TypeError, ValueError):
                pass
    last_message = chat.get("lastMessage") if isinstance(chat, dict) else None
    if isinstance(last_message, dict):
        sender_id = last_message.get("senderId") or last_message.get("sender")
        if sender_id is not None:
            try:
                contact_ids.add(int(sender_id))
            except (TypeError, ValueError):
                pass
    await ensure_user_names_for_ids(list(contact_ids))


def is_supported_chat(chat: dict | None, chat_id: int | None) -> bool:
    if chat_id in (None, 0):
        return False
    if not isinstance(chat, dict):
        return True
    if chat.get("title") == "welcome.saved.dialog.message":
        return False
    return True

def resolve_user_name(uid: int) -> str:
    names = load_user_names()
    return names.get(str(uid), str(uid))


def _resolve_dialog_title_from_participants(chat: dict) -> str:
    participants = chat.get("participants") if isinstance(chat, dict) else None
    if not isinstance(participants, dict):
        return ""
    ignored_ids = {str(item) for item in [chat.get("owner"), max_bridge.get_own_contact_id()] if item is not None}
    for participant_id in participants.keys():
        if str(participant_id) in ignored_ids:
            continue
        name = resolve_user_name(int(participant_id))
        if name and name != str(participant_id):
            return name
    return ""


async def confirm_max_message(chat_id: int, message_id: int | None, timeout_seconds: int = 30) -> bool:
    if not message_id:
        return False
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        try:
            message = await max_bridge.get_message(chat_id, message_id)
            if message and int(message.get("id") or 0) == int(message_id):
                return True
        except Exception:
            pass
        await asyncio.sleep(2)
    return False


def _build_max_message_text(msg: dict) -> tuple[str, str]:
    txt, sid = msg.get("text", ""), msg.get("senderId") or msg.get("sender")
    if sid:
        sid_name = resolve_user_name(sid)
        html_prefix = f"<b>👤 {sid_name}:</b>\n"
        plain_prefix = f"👤 {sid_name}:\n"
    else:
        html_prefix = ""
        plain_prefix = ""
    formatted_text = ""
    if txt:
        formatted_text = format_max_text(txt, msg.get("elements", []))
    html_text = f"{html_prefix}{formatted_text}" if formatted_text else html_prefix
    plain_text = f"{plain_prefix}{txt}" if txt else plain_prefix
    return html_text.strip(), plain_text.strip()


async def _run_tg_send_with_retry(action_name: str, sender):
    network_attempt = 1
    while True:
        try:
            return await sender()
        except TelegramRetryAfter as exc:
            delay = exc.retry_after + 1
            logger.warning(
                "Flood control on %s, sleeping %ss",
                action_name,
                delay,
            )
            await asyncio.sleep(delay)
        except (TelegramNetworkError, asyncio.TimeoutError) as exc:
            if network_attempt >= TG_SEND_NETWORK_RETRY_ATTEMPTS:
                raise
            delay = TG_SEND_NETWORK_RETRY_DELAY_SECONDS * network_attempt
            logger.warning(
                "Retrying Telegram %s after network error attempt=%s/%s delay=%.1fs: %s",
                action_name,
                network_attempt,
                TG_SEND_NETWORK_RETRY_ATTEMPTS,
                delay,
                exc,
            )
            network_attempt += 1
            await asyncio.sleep(delay)


async def _create_forum_topic_with_retry(title: str):
    while True:
        try:
            return await bot.create_forum_topic(TG_GROUP_ID, title)
        except TelegramRetryAfter as exc:
            delay = int(exc.retry_after) + 1
            logger.warning(
                "Flood control on create_forum_topic '%s', sleeping %ss",
                title,
                delay,
            )
            await asyncio.sleep(delay)


async def _send_tg_message_html_safe(*, thread_id: int, html_text: str, plain_text: str) -> Message:
    try:
        return await _run_tg_send_with_retry(
            "send_message",
            lambda: bot.send_message(
                TG_GROUP_ID,
                message_thread_id=thread_id,
                text=html_text,
                parse_mode="HTML",
            ),
        )
    except TelegramBadRequest as exc:
        if not _is_tg_entity_parse_error(exc):
            raise
        logger.warning("Retrying Telegram send_message without HTML parse mode: %s", exc)
        return await _run_tg_send_with_retry(
            "send_message_plain",
            lambda: bot.send_message(
                TG_GROUP_ID,
                message_thread_id=thread_id,
                text=plain_text,
            ),
        )


async def _send_tg_photo_html_safe(
    *,
    thread_id: int,
    photo,
    html_caption: str,
    plain_caption: str,
) -> Message:
    try:
        return await _run_tg_send_with_retry(
            "send_photo",
            lambda: bot.send_photo(
                TG_GROUP_ID,
                photo=photo,
                message_thread_id=thread_id,
                caption=html_caption if html_caption else None,
                parse_mode="HTML",
            ),
        )
    except TelegramBadRequest as exc:
        if not _is_tg_entity_parse_error(exc):
            raise
        logger.warning("Retrying Telegram send_photo without HTML parse mode: %s", exc)
        return await _run_tg_send_with_retry(
            "send_photo_plain",
            lambda: bot.send_photo(
                TG_GROUP_ID,
                photo=photo,
                message_thread_id=thread_id,
                caption=plain_caption if plain_caption else None,
            ),
        )


async def _send_tg_video_html_safe(
    *,
    thread_id: int,
    video,
    html_caption: str,
    plain_caption: str,
    width: int | None = None,
    height: int | None = None,
    duration: int | None = None,
) -> Message:
    try:
        return await _run_tg_send_with_retry(
            "send_video",
            lambda: bot.send_video(
                TG_GROUP_ID,
                video=video,
                message_thread_id=thread_id,
                caption=html_caption if html_caption else None,
                parse_mode="HTML",
                width=width,
                height=height,
                duration=duration,
                supports_streaming=True,
                request_timeout=TG_VIDEO_SEND_TIMEOUT_SECONDS,
            ),
        )
    except TelegramBadRequest as exc:
        if not _is_tg_entity_parse_error(exc):
            raise
        logger.warning("Retrying Telegram send_video without HTML parse mode: %s", exc)
        return await _run_tg_send_with_retry(
            "send_video_plain",
            lambda: bot.send_video(
                TG_GROUP_ID,
                video=video,
                message_thread_id=thread_id,
                caption=plain_caption if plain_caption else None,
                width=width,
                height=height,
                duration=duration,
                supports_streaming=True,
                request_timeout=TG_VIDEO_SEND_TIMEOUT_SECONDS,
            ),
        )


async def _send_tg_document_html_safe(
    *,
    thread_id: int,
    document,
    html_caption: str,
    plain_caption: str,
) -> Message:
    try:
        return await _run_tg_send_with_retry(
            "send_document",
            lambda: bot.send_document(
                TG_GROUP_ID,
                document=document,
                message_thread_id=thread_id,
                caption=html_caption if html_caption else None,
                parse_mode="HTML",
            ),
        )
    except TelegramBadRequest as exc:
        if not _is_tg_entity_parse_error(exc):
            raise
        logger.warning("Retrying Telegram send_document without HTML parse mode: %s", exc)
        return await _run_tg_send_with_retry(
            "send_document_plain",
            lambda: bot.send_document(
                TG_GROUP_ID,
                document=document,
                message_thread_id=thread_id,
                caption=plain_caption if plain_caption else None,
            ),
        )


async def _download_max_video_with_retry(
    *,
    chat_id: int,
    message_id: int,
    attach_index: int,
    attempts: int = 3,
    delay_seconds: float = 2.0,
) -> dict:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await max_bridge.download_video(
                chat_id=chat_id,
                message_id=message_id,
                attach_index=attach_index,
            )
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                break
            logger.warning(
                "Retrying Max video download chat_id=%s message_id=%s attempt=%s/%s after error: %s",
                chat_id,
                message_id,
                attempt,
                attempts,
                exc,
            )
            await asyncio.sleep(delay_seconds)
    if last_error is None:
        raise RuntimeError("video download failed without exception")
    raise last_error


async def _resolve_max_video_urls_with_retry(
    *,
    chat_id: int,
    message_id: int,
    attach_index: int,
    attempts: int = 2,
    delay_seconds: float = 1.0,
) -> dict | None:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await max_bridge.resolve_video_urls(
                chat_id=chat_id,
                message_id=message_id,
                attach_index=attach_index,
            )
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                break
            await asyncio.sleep(delay_seconds)
    logger.warning(
        "Failed to resolve Max video links chat_id=%s message_id=%s attach_index=%s: %s",
        chat_id,
        message_id,
        attach_index,
        last_error,
    )
    return None


def _video_source_quality(source_key: str) -> int:
    matches = re.findall(r"\d+", source_key)
    return int(matches[-1]) if matches else 0


def _build_max_video_link_text(video_info: dict | None) -> tuple[str, str]:
    if not isinstance(video_info, dict):
        return "", ""

    links: list[tuple[str, str]] = []
    seen_urls: set[str] = set()

    def add_link(label: str, url: object) -> None:
        if not isinstance(url, str) or not url or url in seen_urls:
            return
        seen_urls.add(url)
        links.append((label, url))

    add_link("открыть в Max", video_info.get("external_url"))
    add_link("скачать файл", video_info.get("selected_url"))

    sources = video_info.get("sources")
    if isinstance(sources, dict) and not any(label == "скачать файл" for label, _ in links):
        candidates = [
            (str(key), value)
            for key, value in sources.items()
            if isinstance(value, str) and value
        ]
        mp4_candidates = [
            item
            for item in candidates
            if item[0].upper().startswith("MP4")
        ]
        selected = None
        if mp4_candidates:
            selected = sorted(
                mp4_candidates,
                key=lambda item: _video_source_quality(item[0]),
                reverse=True,
            )[0]
        elif candidates:
            selected = sorted(candidates, key=lambda item: item[0])[0]
        if selected is not None:
            add_link("скачать файл", selected[1])

    if not links:
        return "", ""

    html_links = [
        f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'
        for label, url in links[:2]
    ]
    plain_links = [f"{label}: {url}" for label, url in links[:2]]
    return (
        "🔗 Видео из Max: " + " · ".join(html_links),
        "Видео из Max: " + " | ".join(plain_links),
    )


async def _send_max_video_link_if_available(
    *,
    video_info: dict | None,
    thread_id: int,
) -> Message | None:
    html_text, plain_text = _build_max_video_link_text(video_info)
    if not html_text:
        return None
    try:
        return await _send_tg_message_html_safe(
            thread_id=thread_id,
            html_text=html_text,
            plain_text=plain_text,
        )
    except Exception:
        logger.exception(
            "Failed to send Max video link message chat_id=%s message_id=%s",
            video_info.get("chat_id") if isinstance(video_info, dict) else None,
            video_info.get("message_id") if isinstance(video_info, dict) else None,
        )
    return None


def _append_video_fallback_note(html_caption: str, plain_caption: str) -> tuple[str, str]:
    note_html = "📹 <i>[Видео из Max не удалось отправить как файл]</i>"
    note_plain = "📹 [Видео из Max не удалось отправить как файл]"
    if not html_caption:
        return note_html, note_plain

    html_candidate = f"{html_caption}\n\n{note_html}"
    plain_candidate = f"{plain_caption}\n\n{note_plain}" if plain_caption else note_plain
    if len(html_candidate) <= 1024 and len(plain_candidate) <= 1024:
        return html_candidate, plain_candidate
    return html_caption, plain_caption


async def _send_max_video_thumbnail_fallback(
    *,
    attach: dict,
    thread_id: int,
    html_caption: str,
    plain_caption: str,
) -> Message:
    html_caption, plain_caption = _append_video_fallback_note(html_caption, plain_caption)
    thumbnail_url = attach.get("thumbnail")
    if isinstance(thumbnail_url, str) and thumbnail_url:
        try:
            return await _send_tg_photo_html_safe(
                thread_id=thread_id,
                photo=thumbnail_url,
                html_caption=html_caption,
                plain_caption=plain_caption,
            )
        except Exception:
            logger.exception("Failed to send Max video thumbnail fallback")
    html_fallback = html_caption or "📹 <i>[Видео из Max временно недоступно, отправлено без превью]</i>"
    plain_fallback = plain_caption or "📹 [Видео из Max временно недоступно, отправлено без превью]"
    return await _send_tg_message_html_safe(
        thread_id=thread_id,
        html_text=html_fallback,
        plain_text=plain_fallback,
    )


async def _delete_tg_messages(mappings: list[dict]) -> None:
    for mapping in mappings:
        try:
            await bot.delete_message(mapping["tg_chat_id"], mapping["tg_message_id"])
        except TelegramBadRequest as exc:
            if "message to delete not found" in str(exc).lower():
                continue
            logger.warning("Failed to delete Telegram message %s: %s", mapping["tg_message_id"], exc)


def _max_reaction_count(counter: dict) -> int:
    try:
        return int(counter.get("count") or 0)
    except (TypeError, ValueError):
        return 0


def _max_reaction_info_emoji(reaction_info: dict | None) -> str | None:
    if not isinstance(reaction_info, dict):
        return None
    counters = reaction_info.get("counters")
    if not isinstance(counters, list):
        return None

    candidates: list[dict] = []
    for counter in counters:
        if not isinstance(counter, dict):
            continue
        reaction = counter.get("reaction")
        if isinstance(reaction, str) and reaction and _max_reaction_count(counter) > 0:
            candidates.append(counter)
    if not candidates:
        return None

    selected = max(candidates, key=_max_reaction_count)
    return str(selected["reaction"])


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _max_message_sender_id(msg: dict | None) -> int | None:
    if not isinstance(msg, dict):
        return None
    return _int_or_none(msg.get("senderId") or msg.get("sender"))


def _max_message_is_from_own(msg: dict | None, own_user_id: int | None = None) -> bool:
    sender_id = _max_message_sender_id(msg)
    if sender_id is None:
        return False
    own_id = own_user_id if own_user_id is not None else max_bridge.get_own_contact_id()
    return own_id is not None and sender_id == own_id


def _max_message_viewed_by_other_participant(
    *,
    chat_info: dict | None,
    msg: dict | None,
    own_user_id: int | None = None,
) -> bool:
    if not isinstance(chat_info, dict) or not isinstance(msg, dict):
        return False

    message_time = _int_or_none(msg.get("time"))
    if message_time is None:
        return False

    own_id = own_user_id if own_user_id is not None else max_bridge.get_own_contact_id()
    if own_id is None or not _max_message_is_from_own(msg, own_user_id=own_id):
        return False

    participants = chat_info.get("participants")
    if not isinstance(participants, dict):
        return False

    for participant_id, read_time_value in participants.items():
        participant = _int_or_none(participant_id)
        read_time = _int_or_none(read_time_value)
        if participant is None or read_time is None or participant == own_id:
            continue
        if read_time >= message_time:
            return True
    return False


def _max_message_state_emoji(
    *,
    msg: dict | None,
    reaction_info: dict | None,
    chat_info: dict | None = None,
    own_user_id: int | None = None,
) -> str | None:
    reaction_emoji = _max_reaction_info_emoji(reaction_info)
    if reaction_emoji:
        return reaction_emoji
    if TG_VIEWED_REACTION and _max_message_viewed_by_other_participant(
        chat_info=chat_info,
        msg=msg,
        own_user_id=own_user_id,
    ):
        return TG_VIEWED_REACTION
    return None


async def _load_max_reaction_info_map(chat_id: int, message_ids: list[int]) -> dict[int, dict]:
    ids = sorted({int(message_id) for message_id in message_ids if message_id})
    if not ids:
        return {}
    try:
        response = await max_bridge.get_reactions(chat_id, ids)
    except Exception:
        logger.exception("Failed to load Max reactions chat_id=%s message_ids=%s", chat_id, ids[:10])
        return {}

    raw_reactions = response.get("messagesReactions") if isinstance(response, dict) else None
    if not isinstance(raw_reactions, dict):
        return {}

    result: dict[int, dict] = {}
    for raw_message_id, reaction_info in raw_reactions.items():
        message_id = _int_or_none(raw_message_id)
        if message_id is not None and isinstance(reaction_info, dict):
            result[message_id] = reaction_info
    return result


async def _set_tg_reaction(
    *,
    tg_chat_id: int,
    tg_message_id: int,
    emoji: str | None,
) -> None:
    global _last_tg_reaction_at

    key = (tg_chat_id, tg_message_id)
    normalized = emoji or ""
    if _known_tg_message_reactions.get(key) == normalized:
        return
    if emoji and emoji in _invalid_tg_reaction_emojis:
        return

    reaction = [ReactionTypeEmoji(emoji=emoji)] if emoji else []
    network_attempt = 1
    async with _tg_reaction_lock:
        while True:
            elapsed = asyncio.get_running_loop().time() - _last_tg_reaction_at
            if elapsed < TG_REACTION_MIN_INTERVAL_SECONDS:
                await asyncio.sleep(TG_REACTION_MIN_INTERVAL_SECONDS - elapsed)
            try:
                remember_tg_reaction_update(tg_chat_id, tg_message_id)
                await bot.set_message_reaction(
                    chat_id=tg_chat_id,
                    message_id=tg_message_id,
                    reaction=reaction,
                )
                _last_tg_reaction_at = asyncio.get_running_loop().time()
                _known_tg_message_reactions[key] = normalized
                return
            except TelegramRetryAfter as exc:
                delay = exc.retry_after + 1
                _last_tg_reaction_at = asyncio.get_running_loop().time()
                logger.warning(
                    "Flood control on set_message_reaction message_id=%s, sleeping %ss",
                    tg_message_id,
                    delay,
                )
                await asyncio.sleep(delay)
            except (TelegramNetworkError, asyncio.TimeoutError) as exc:
                _last_tg_reaction_at = asyncio.get_running_loop().time()
                if network_attempt >= TG_SEND_NETWORK_RETRY_ATTEMPTS:
                    logger.warning(
                        "Failed to set Telegram reaction after network retries message_id=%s emoji=%r: %s",
                        tg_message_id,
                        emoji,
                        exc,
                    )
                    return
                delay = TG_SEND_NETWORK_RETRY_DELAY_SECONDS * network_attempt
                logger.warning(
                    "Retrying Telegram set_message_reaction after network error "
                    "message_id=%s emoji=%r attempt=%s/%s delay=%.1fs: %s",
                    tg_message_id,
                    emoji,
                    network_attempt,
                    TG_SEND_NETWORK_RETRY_ATTEMPTS,
                    delay,
                    exc,
                )
                network_attempt += 1
                await asyncio.sleep(delay)
            except TelegramBadRequest as exc:
                text = str(exc).lower()
                if not emoji and "reaction_empty" in text:
                    _last_tg_reaction_at = asyncio.get_running_loop().time()
                    _known_tg_message_reactions[key] = normalized
                    return
                if emoji and "reaction_invalid" in text:
                    _invalid_tg_reaction_emojis.add(emoji)
                logger.warning(
                    "Failed to set Telegram reaction message_id=%s emoji=%r: %s",
                    tg_message_id,
                    emoji,
                    exc,
                )
                _last_tg_reaction_at = asyncio.get_running_loop().time()
                return
            except Exception:
                logger.exception("Failed to set Telegram reaction message_id=%s", tg_message_id)
                _last_tg_reaction_at = asyncio.get_running_loop().time()
                return


async def _sync_max_message_state_to_tg(
    max_chat_id: int,
    max_message_id: int,
    msg: dict | None,
    reaction_info: dict | None,
    *,
    chat_info: dict | None = None,
    clear_when_empty: bool = False,
) -> None:
    emoji = _max_message_state_emoji(
        msg=msg,
        reaction_info=reaction_info,
        chat_info=chat_info,
    )
    if not emoji and not clear_when_empty:
        return

    mappings = await db.get_tg_messages_for_max_message(max_chat_id, max_message_id)
    if not mappings:
        return
    first_mapping = mappings[0]
    tg_key = (first_mapping["tg_chat_id"], first_mapping["tg_message_id"])
    if (
        not emoji
        and clear_when_empty
        and _known_tg_message_reactions.get(tg_key) in (None, "")
        and not _max_message_is_from_own(msg)
    ):
        return

    await _set_tg_reaction(
        tg_chat_id=first_mapping["tg_chat_id"],
        tg_message_id=first_mapping["tg_message_id"],
        emoji=emoji,
    )


async def _store_max_to_tg_mapping(max_chat_id: int, max_message_id: int, thread_id: int, sent_messages: list[Message]) -> None:
    tg_message_ids = [message.message_id for message in sent_messages]
    if not tg_message_ids:
        return
    await db.replace_max_message_mappings(
        max_chat_id=max_chat_id,
        max_message_id=max_message_id,
        tg_chat_id=TG_GROUP_ID,
        tg_thread_id=thread_id,
        tg_message_ids=tg_message_ids,
    )


def _get_renderable_max_attaches(msg: dict) -> list[dict]:
    attaches = msg.get("attaches", [])
    if not isinstance(attaches, list):
        return []
    supported_types = {"PHOTO", "VIDEO", "AUDIO", "FILE", "STICKER", "CALL"}
    return [
        attach
        for attach in attaches
        if isinstance(attach, dict)
        and (attach.get("_type") in supported_types or "photoToken" in attach)
    ]


def _get_forwarded_link_message(msg: dict) -> dict | None:
    link = msg.get("link")
    if not isinstance(link, dict) or link.get("type") != "FORWARD":
        return None
    linked_msg = link.get("message")
    return linked_msg if isinstance(linked_msg, dict) else None


def _build_forwarded_message_text(msg: dict, linked_msg: dict) -> tuple[str, str]:
    sender = msg.get("senderId") or msg.get("sender")
    linked_sender = linked_msg.get("senderId") or linked_msg.get("sender")
    sender_name = resolve_user_name(int(sender)) if sender is not None else "MAX"
    linked_sender_name = resolve_user_name(int(linked_sender)) if linked_sender is not None else "MAX"
    linked_text = linked_msg.get("text") or ""
    formatted_linked_text = format_max_text(linked_text, linked_msg.get("elements", [])) if linked_text else ""
    html_text = (
        f"<b>👤 {html.escape(sender_name)}:</b>\n"
        f"<i>↪️ Пересланное сообщение от {html.escape(linked_sender_name)}</i>"
    )
    plain_text = f"👤 {sender_name}:\n↪️ Пересланное сообщение от {linked_sender_name}"
    if formatted_linked_text:
        html_text = f"{html_text}\n{formatted_linked_text}"
        plain_text = f"{plain_text}\n{linked_text}"
    return html_text, plain_text


async def _download_forwarded_photo(chat_id: int, message_id: int, attach_index: int, attach: dict) -> dict:
    base_url = attach.get("baseUrl")
    if not isinstance(base_url, str) or not base_url:
        raise ValueError("Forwarded PHOTO attach does not contain baseUrl")
    output = Path("data") / suggested_photo_name(chat_id=chat_id, message_id=message_id)
    saved_path, content_type = await asyncio.to_thread(download_to_file, base_url, output)
    renamed = saved_path.with_name(
        suggested_photo_name(
            chat_id=chat_id,
            message_id=message_id,
            content_type=content_type,
        )
    )
    if renamed != saved_path:
        saved_path.rename(renamed)
        saved_path = renamed
    return {
        "attach_index": attach_index,
        "saved_path": str(saved_path),
        "content_type": content_type,
    }


def _format_max_call_text(attach: dict) -> tuple[str, str]:
    call_type = str(attach.get("callType") or "").upper()
    call_label = "видеозвонок" if call_type == "VIDEO" else "аудиозвонок"
    hangup_type = str(attach.get("hangupType") or "").upper()
    if hangup_type == "CANCELED":
        plain_text = f"📞 Пропущенный {call_label}"
    else:
        plain_text = f"📞 {call_label.capitalize()}"

    duration = attach.get("duration")
    if isinstance(duration, int) and duration > 0:
        seconds = duration // 1000 if duration > 1000 else duration
        minutes, rest = divmod(seconds, 60)
        plain_text = f"{plain_text}, длительность {minutes}:{rest:02d}"

    return f"<i>{html.escape(plain_text)}</i>", plain_text


async def _send_max_message_to_topic(chat_id: int, msg: dict, thread_id: int) -> list[Message]:
    sid = msg.get("senderId") or msg.get("sender")
    if sid is not None:
        await ensure_user_names_for_ids([sid])
    linked_msg = _get_forwarded_link_message(msg)
    if linked_msg is not None:
        linked_sender = linked_msg.get("senderId") or linked_msg.get("sender")
        if linked_sender is not None:
            await ensure_user_names_for_ids([linked_sender])
    render_msg = linked_msg if linked_msg is not None and not msg.get("text") and not msg.get("attaches") else msg
    if render_msg is linked_msg:
        full_text, plain_text = _build_forwarded_message_text(msg, linked_msg)
    else:
        full_text, plain_text = _build_max_message_text(msg)
    logger.debug(
        "Forwarding Max message chat_id=%s sender_id=%s has_text=%s attaches=%s",
        chat_id,
        sid,
        bool(plain_text),
        len(render_msg.get("attaches", [])),
    )

    sent_messages: list[Message] = []
    attaches = _get_renderable_max_attaches(render_msg)
    attach_type_offsets = {"PHOTO": 0, "VIDEO": 0, "AUDIO": 0, "FILE": 0, "STICKER": 0, "CALL": 0}
    text_sent = False
    mid = msg.get("id")

    for idx, attach in enumerate(attaches):
        caption = ""
        plain_caption = ""
        if not text_sent and full_text and len(full_text) <= 1024 and len(plain_text) <= 1024:
            caption = full_text
            plain_caption = plain_text
            text_sent = True

        if attach.get("_type") == "PHOTO" or "photoToken" in attach:
            if mid:
                photo_index = attach_type_offsets["PHOTO"]
                attach_type_offsets["PHOTO"] += 1
                try:
                    if render_msg is linked_msg:
                        photo_info = await _download_forwarded_photo(chat_id, int(mid), photo_index, attach)
                    else:
                        photo_info = await max_bridge.download_photo(
                            chat_id=chat_id,
                            message_id=mid,
                            attach_index=photo_index,
                            attach=attach,
                        )
                    photo_path = photo_info["saved_path"]
                    try:
                        sent_messages.append(
                            await _send_tg_photo_html_safe(
                                thread_id=thread_id,
                                photo=FSInputFile(photo_path),
                                html_caption=caption,
                                plain_caption=plain_caption,
                            )
                        )
                    finally:
                        if os.path.exists(photo_path):
                            os.remove(photo_path)
                except Exception:
                    logger.exception(
                        "Failed to download/send Max photo chat_id=%s message_id=%s",
                        chat_id,
                        mid,
                    )
                    if caption or plain_caption:
                        sent_messages.append(
                            await _send_tg_message_html_safe(
                                thread_id=thread_id,
                                html_text=caption or plain_caption,
                                plain_text=plain_caption or caption,
                            )
                        )
                        text_sent = True
        elif attach.get("_type") == "VIDEO":
            if mid:
                video_index = attach_type_offsets["VIDEO"]
                attach_type_offsets["VIDEO"] += 1
                video_path = None
                video_info = None
                try:
                    video_info = await _download_max_video_with_retry(
                        chat_id=chat_id,
                        message_id=mid,
                        attach_index=video_index,
                    )
                    video_path = video_info["saved_path"]
                    duration = attach.get("duration")
                    sent_messages.append(
                        await _send_tg_video_html_safe(
                            thread_id=thread_id,
                            video=FSInputFile(video_path),
                            html_caption=caption,
                            plain_caption=plain_caption,
                            width=attach.get("width"),
                            height=attach.get("height"),
                            duration=int(duration / 1000) if isinstance(duration, int) and duration > 0 else None,
                        )
                    )
                    link_message = await _send_max_video_link_if_available(
                        video_info=video_info,
                        thread_id=thread_id,
                    )
                    if link_message is not None:
                        sent_messages.append(link_message)
                    text_sent = True
                except Exception:
                    logger.exception("Failed to download/send Max video chat_id=%s message_id=%s", chat_id, mid)
                    if video_info is None:
                        video_info = await _resolve_max_video_urls_with_retry(
                            chat_id=chat_id,
                            message_id=int(mid),
                            attach_index=video_index,
                        )
                    sent_messages.append(
                        await _send_max_video_thumbnail_fallback(
                            attach=attach,
                            thread_id=thread_id,
                            html_caption=caption,
                            plain_caption=plain_caption,
                        )
                    )
                    link_message = await _send_max_video_link_if_available(
                        video_info=video_info,
                        thread_id=thread_id,
                    )
                    if link_message is not None:
                        sent_messages.append(link_message)
                    text_sent = True
                finally:
                    if video_path and os.path.exists(video_path):
                        os.remove(video_path)
        elif attach.get("_type") == "AUDIO":
            if mid:
                audio_index = attach_type_offsets["AUDIO"]
                attach_type_offsets["AUDIO"] += 1
                audio_path = None
                try:
                    audio_info = await max_bridge.download_audio(
                        chat_id=chat_id,
                        message_id=mid,
                        attach_index=audio_index,
                    )
                    audio_path = audio_info["saved_path"]
                    sent_messages.append(
                        await _send_tg_document_html_safe(
                            thread_id=thread_id,
                            document=FSInputFile(
                                audio_path,
                                filename=os.path.basename(audio_path),
                            ),
                            html_caption=caption,
                            plain_caption=plain_caption,
                        )
                    )
                    text_sent = True
                except Exception:
                    logger.exception("Failed to download/send Max audio chat_id=%s message_id=%s", chat_id, mid)
                    html_fallback = caption or "🎧 <i>[Аудиофайл из Max не удалось скачать]</i>"
                    plain_fallback = plain_caption or "🎧 [Аудиофайл из Max не удалось скачать]"
                    sent_messages.append(
                        await _send_tg_message_html_safe(
                            thread_id=thread_id,
                            html_text=html_fallback,
                            plain_text=plain_fallback,
                        )
                    )
                    text_sent = True
                finally:
                    if audio_path and os.path.exists(audio_path):
                        os.remove(audio_path)
        elif attach.get("_type") == "FILE":
            if mid:
                file_index = attach_type_offsets["FILE"]
                attach_type_offsets["FILE"] += 1
                file_path = None
                try:
                    file_info = await max_bridge.download_file(
                        chat_id=chat_id,
                        message_id=mid,
                        attach_index=file_index,
                    )
                    file_path = file_info["saved_path"]
                    sent_messages.append(
                        await _send_tg_document_html_safe(
                            thread_id=thread_id,
                            document=FSInputFile(
                                file_path,
                                filename=file_info.get("name") or os.path.basename(file_path),
                            ),
                            html_caption=caption,
                            plain_caption=plain_caption,
                        )
                    )
                    text_sent = True
                except Exception:
                    logger.exception("Failed to download/send Max file chat_id=%s message_id=%s", chat_id, mid)
                    html_fallback = caption or "📎 <i>[Файл из Max не удалось скачать]</i>"
                    plain_fallback = plain_caption or "📎 [Файл из Max не удалось скачать]"
                    sent_messages.append(
                        await _send_tg_message_html_safe(
                            thread_id=thread_id,
                            html_text=html_fallback,
                            plain_text=plain_fallback,
                        )
                    )
                    text_sent = True
                finally:
                    if file_path and os.path.exists(file_path):
                        os.remove(file_path)
        elif attach.get("_type") == "STICKER":
            if mid:
                sticker_index = attach_type_offsets["STICKER"]
                attach_type_offsets["STICKER"] += 1
                sticker_paths: list[str] = []
                try:
                    sticker_info = await max_bridge.download_sticker(
                        chat_id=chat_id,
                        message_id=mid,
                        attach_index=sticker_index,
                    )
                    preview_path = sticker_info.get("preview_path")
                    if preview_path:
                        sticker_paths.append(preview_path)
                        sent_messages.append(
                            await _send_tg_photo_html_safe(
                                thread_id=thread_id,
                                photo=FSInputFile(preview_path),
                                html_caption=caption,
                                plain_caption=plain_caption,
                            )
                        )
                        text_sent = True
                    for key in ("lottie_gz_path", "lottie_json_path"):
                        path = sticker_info.get(key)
                        if path:
                            sticker_paths.append(path)
                except Exception:
                    logger.exception("Failed to download/send Max sticker chat_id=%s message_id=%s", chat_id, mid)
                    html_fallback = caption or "🙂 <i>[Стикер из Max не удалось скачать]</i>"
                    plain_fallback = plain_caption or "🙂 [Стикер из Max не удалось скачать]"
                    sent_messages.append(
                        await _send_tg_message_html_safe(
                            thread_id=thread_id,
                            html_text=html_fallback,
                            plain_text=plain_fallback,
                        )
                    )
                    text_sent = True
                finally:
                    for path in sticker_paths:
                        if os.path.exists(path):
                            os.remove(path)
        elif attach.get("_type") == "CALL":
            call_html, call_plain = _format_max_call_text(attach)
            if caption:
                html_text = f"{caption}\n\n{call_html}"
                plain_text = f"{plain_caption}\n\n{call_plain}" if plain_caption else call_plain
            else:
                html_text = f"{full_text}\n\n{call_html}" if full_text else call_html
                plain_text = f"{plain_text}\n\n{call_plain}" if plain_text else call_plain
            sent_messages.append(
                await _send_tg_message_html_safe(
                    thread_id=thread_id,
                    html_text=html_text,
                    plain_text=plain_text,
                )
            )
            text_sent = True

    if render_msg.get("attaches") and not attaches and not full_text:
        attach_types = [
            attach.get("_type")
            for attach in render_msg.get("attaches", [])
            if isinstance(attach, dict)
        ]
        logger.info(
            "Skipping unsupported Max attachments chat_id=%s message_id=%s attach_types=%s",
            chat_id,
            mid,
            attach_types,
        )

    if full_text and not text_sent:
        sent_messages.append(
            await _send_tg_message_html_safe(
                thread_id=thread_id,
                html_text=full_text,
                plain_text=plain_text,
            )
        )
    return sent_messages


async def _upsert_max_message_in_topic(chat_id: int, msg: dict, thread_id: int) -> None:
    mid = int(msg.get("id") or 0)
    if not mid:
        return
    existing = await db.get_tg_messages_for_max_message(chat_id, mid)
    if not existing:
        sent_messages = await _send_max_message_to_topic(chat_id, msg, thread_id)
        await _store_max_to_tg_mapping(chat_id, mid, thread_id, sent_messages)
        await _sync_max_message_state_to_tg(chat_id, mid, msg, msg.get("reactionInfo"), clear_when_empty=True)
        return
    if _get_forwarded_link_message(msg) is not None and not msg.get("text") and not msg.get("attaches"):
        await _delete_tg_messages(existing)
        sent_messages = await _send_max_message_to_topic(chat_id, msg, thread_id)
        await _store_max_to_tg_mapping(chat_id, mid, thread_id, sent_messages)
        await _sync_max_message_state_to_tg(chat_id, mid, msg, msg.get("reactionInfo"), clear_when_empty=True)
        return

    full_text, _ = _build_max_message_text(msg)
    attaches = _get_renderable_max_attaches(msg)
    try:
        if len(existing) == 1 and not attaches:
            await bot.edit_message_text(
                chat_id=existing[0]["tg_chat_id"],
                message_id=existing[0]["tg_message_id"],
                text=full_text or " ",
                parse_mode="HTML",
            )
            await _sync_max_message_state_to_tg(chat_id, mid, msg, msg.get("reactionInfo"), clear_when_empty=True)
            return
        if len(existing) == 1 and len(attaches) == 1 and attaches[0].get("_type") in {"PHOTO", "VIDEO", "AUDIO", "FILE", "STICKER"} and len(full_text) <= 1024:
            await bot.edit_message_caption(
                chat_id=existing[0]["tg_chat_id"],
                message_id=existing[0]["tg_message_id"],
                caption=full_text or None,
                parse_mode="HTML",
            )
            await _sync_max_message_state_to_tg(chat_id, mid, msg, msg.get("reactionInfo"), clear_when_empty=True)
            return
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        logger.warning("Failed to edit Telegram message for Max edit; replacing instead: %s", exc)

    await _delete_tg_messages(existing)
    sent_messages = await _send_max_message_to_topic(chat_id, msg, thread_id)
    await _store_max_to_tg_mapping(chat_id, mid, thread_id, sent_messages)
    await _sync_max_message_state_to_tg(chat_id, mid, msg, msg.get("reactionInfo"), clear_when_empty=True)


async def replay_recent_messages_to_topic(chat_id: int, thread_id: int, count: int = BACKFILL_MESSAGES_PER_CHAT) -> None:
    try:
        last_message = await max_bridge.get_last_message(chat_id)
    except Exception as exc:
        logger.info("Skipping history replay for Max chat_id=%s: no last message (%s)", chat_id, exc)
        return
    from_message = int(last_message.get("id") or 0) if isinstance(last_message, dict) else 0
    if not from_message:
        return

    messages: list[dict] = []
    seen_ids: set[int] = set()
    cursor = from_message
    remaining = count
    while True:
        page_size = BACKFILL_PAGE_SIZE if count <= 0 else min(BACKFILL_PAGE_SIZE, remaining)
        if page_size <= 0:
            break
        history = await max_bridge.get_chat_history(
            chat_id=chat_id,
            count=page_size,
            from_message=cursor,
            direction="backward",
            get_chat=False,
            get_messages=True,
        )
        page_messages = history.get("messages") or []
        if not page_messages:
            break
        next_cursor = cursor
        added = 0
        for msg in page_messages:
            message_id = int(msg.get("id") or 0)
            if not message_id or message_id in seen_ids:
                continue
            seen_ids.add(message_id)
            messages.append(msg)
            next_cursor = min(next_cursor, message_id)
            added += 1
        if count > 0:
            remaining -= added
            if remaining <= 0:
                break
        if not added or next_cursor == cursor or len(page_messages) < page_size:
            break
        cursor = next_cursor

    messages.sort(key=lambda msg: int(msg.get("id") or 0))
    try:
        chat_info = await max_bridge.get_chat_info(chat_id)
    except Exception as exc:
        logger.warning("Failed to load Max chat info for state sync chat_id=%s: %s", chat_id, exc)
        chat_info = None
    reaction_infos = await _load_max_reaction_info_map(
        chat_id,
        [int(msg.get("id") or 0) for msg in messages],
    )
    logger.info("Replaying %s Max messages to Telegram topic chat_id=%s thread_id=%s", len(messages), chat_id, thread_id)
    for msg in messages:
        message_id = int(msg.get("id") or 0)
        if not message_id:
            continue
        reaction_info = reaction_infos[message_id] if message_id in reaction_infos else msg.get("reactionInfo")
        existing = await db.get_tg_messages_for_max_message(chat_id, message_id)
        if existing:
            await _sync_max_message_state_to_tg(
                chat_id,
                message_id,
                msg,
                reaction_info,
                chat_info=chat_info,
                clear_when_empty=True,
            )
            continue
        try:
            sent_messages = await _send_max_message_to_topic(chat_id, msg, thread_id)
            await _store_max_to_tg_mapping(chat_id, message_id, thread_id, sent_messages)
            await _sync_max_message_state_to_tg(
                chat_id,
                message_id,
                msg,
                reaction_info,
                chat_info=chat_info,
                clear_when_empty=True,
            )
            await asyncio.sleep(0.1)
        except Exception as exc:
            logger.warning(
                "Skipping backfill message chat_id=%s message_id=%s: %s",
                chat_id,
                message_id,
                exc,
            )


async def sync_recent_message_states(count: int = MAX_STATE_SYNC_MESSAGES_PER_CHAT) -> None:
    if count <= 0:
        return

    async with aiosqlite.connect(db.db_path) as conn:
        async with conn.execute("SELECT max_chat_id FROM chat_mapping ORDER BY chat_name, max_chat_id") as cursor:
            rows = await cursor.fetchall()

    for (chat_id_raw,) in rows:
        chat_id = int(chat_id_raw)
        try:
            last_message = await max_bridge.get_last_message(chat_id)
        except Exception as exc:
            logger.info("Skipping state sync for Max chat_id=%s: no last message (%s)", chat_id, exc)
            continue

        from_message = int(last_message.get("id") or 0) if isinstance(last_message, dict) else 0
        if not from_message:
            continue

        try:
            history = await max_bridge.get_chat_history(
                chat_id=chat_id,
                count=count,
                from_message=from_message,
                direction="backward",
                get_chat=False,
                get_messages=True,
            )
        except Exception:
            logger.exception("Failed to load recent Max history for state sync chat_id=%s", chat_id)
            continue

        messages = [msg for msg in (history.get("messages") or []) if isinstance(msg, dict)]
        if not messages:
            continue

        try:
            chat_info = await max_bridge.get_chat_info(chat_id)
        except Exception:
            logger.exception("Failed to load Max chat info for state sync chat_id=%s", chat_id)
            chat_info = None

        reaction_infos = await _load_max_reaction_info_map(
            chat_id,
            [int(msg.get("id") or 0) for msg in messages],
        )
        for msg in messages:
            message_id = int(msg.get("id") or 0)
            if not message_id:
                continue
            existing = await db.get_tg_messages_for_max_message(chat_id, message_id)
            if not existing:
                continue
            await _sync_max_message_state_to_tg(
                chat_id,
                message_id,
                msg,
                reaction_infos[message_id] if message_id in reaction_infos else msg.get("reactionInfo"),
                chat_info=chat_info,
                clear_when_empty=True,
            )
            await asyncio.sleep(0.05)


async def sync_recent_message_states_forever(interval_seconds: int = MAX_STATE_SYNC_INTERVAL_SECONDS) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await sync_recent_message_states()
        except Exception:
            logger.exception("Periodic Max message state sync failed")


async def load_mapped_chat_ids() -> list[int]:
    async with aiosqlite.connect(db.db_path) as conn:
        async with conn.execute("SELECT max_chat_id FROM chat_mapping ORDER BY max_chat_id") as cursor:
            rows = await cursor.fetchall()
    return [int(row[0]) for row in rows]


async def backfill_existing_topics(
    *,
    count: int = STARTUP_BACKFILL_MESSAGES_PER_CHAT,
    chats_limit: int = STARTUP_BACKFILL_CHATS_LIMIT,
) -> None:
    async with aiosqlite.connect(db.db_path) as conn:
        async with conn.execute(
            "SELECT max_chat_id, tg_thread_id, COALESCE(chat_name, '') FROM chat_mapping ORDER BY chat_name, max_chat_id"
        ) as cursor:
            rows = await cursor.fetchall()
    if not rows:
        return

    if count <= 0 or chats_limit <= 0:
        logger.info("Startup message backfill skipped (count=%s chats_limit=%s)", count, chats_limit)
        return

    try:
        chats = max_bridge.get_login_chats()
        if not chats:
            chats = await asyncio.wait_for(max_bridge.list_chats(), timeout=20)
    except Exception as exc:
        logger.warning("Failed to load Max chats for startup backfill ordering: %s", exc)
        chats = []

    activity = _build_chat_activity_index(chats)
    rows.sort(key=lambda row: activity.get(int(row[0]), 0), reverse=True)
    selected_rows = rows[:chats_limit]

    replay_label = str(count)
    logger.info(
        "Startup backfill for %s recent topics (of %s mapped); messages_per_chat=%s",
        len(selected_rows),
        len(rows),
        replay_label,
    )
    for chat_id, thread_id, chat_name in selected_rows:
        try:
            await replay_recent_messages_to_topic(int(chat_id), int(thread_id), count=count)
        except Exception as exc:
            logger.exception(
                "Startup backfill failed for Max chat_id=%s topic=%s (%s): %s",
                chat_id,
                thread_id,
                chat_name,
                exc,
            )
        await asyncio.sleep(0.1)


async def backfill_startup_topics_async() -> None:
    if not STARTUP_BACKFILL_ENABLED:
        logger.info("Startup message backfill disabled via STARTUP_BACKFILL_ENABLED")
        return
    await backfill_existing_topics(
        count=STARTUP_BACKFILL_MESSAGES_PER_CHAT,
        chats_limit=STARTUP_BACKFILL_CHATS_LIMIT,
    )


async def ensure_chat_topic(chat_id: int, chat_info: dict | None = None, replay_recent_history: bool = False) -> int | None:
    if not is_supported_chat(chat_info, chat_id):
        return None
    tid = await db.get_thread_id(chat_id)
    if tid:
        return tid

    lock = _chat_topic_locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        tid = await db.get_thread_id(chat_id)
        if tid:
            return tid

        if chat_info is None:
            try:
                chat_info = await max_bridge.get_chat_info(chat_id)
            except Exception as exc:
                logger.warning("Failed to load Max chat info for chat_id=%s: %s", chat_id, exc)
                return None
        if not chat_info:
            logger.warning("Failed to load Max chat info for chat_id=%s", chat_id)
            return None

        await ensure_user_names_for_chat(chat_info)
        title = get_chat_title(chat_info, chat_id)
        logger.info("Creating Telegram topic for Max chat %s: %s", chat_id, title)
        topic = await _create_forum_topic_with_retry(title)
        tid = topic.message_thread_id
        await db.save_mapping(chat_id, tid, title)
        if TG_FORUM_TOPIC_CREATE_DELAY_SECONDS > 0:
            await asyncio.sleep(TG_FORUM_TOPIC_CREATE_DELAY_SECONDS)

        if replay_recent_history:
            try:
                await replay_recent_messages_to_topic(chat_id, tid)
            except Exception as exc:
                logger.exception("Failed to replay recent Max history for chat_id=%s: %s", chat_id, exc)

        return tid

async def on_max_message_event(event: MaxMessageEvent) -> None:
    cid = event.chat_id
    msg = event.message or {}
    if cid is None:
        logger.debug("Skipping Max message event without chat id: %s", event)
        return
    for attach in msg.get("attaches") or []:
        if isinstance(attach, dict) and attach.get("_type") == "AUDIO":
            logger.info(
                "Max incoming AUDIO attach chat_id=%s message_id=%s payload=%s",
                cid,
                event.message_id,
                attach,
            )
    if is_recent_bridge_message(event.message_id):
        logger.info("Skipping echoed bridge message from Max chat_id=%s message_id=%s", cid, event.message_id)
        return

    tid = await ensure_chat_topic(cid)
    if tid is None:
        logger.warning("Skipping Max message for chat %s: topic creation failed", cid)
        return

    await max_bridge.ensure_chat_subscription(cid)
    try:
        await _upsert_max_message_in_topic(cid, msg, tid)
    except Exception as exc:
        logger.exception("Max->TG message handling failed chat_id=%s message_id=%s: %s", cid, event.message_id, exc)


async def on_max_delete_event(event: MaxDeleteEvent) -> None:
    if event.chat_id is None:
        return
    for message_id in event.message_ids:
        mappings = await db.get_tg_messages_for_max_message(event.chat_id, message_id)
        if not mappings:
            continue
        await _delete_tg_messages(mappings)
        await db.delete_max_message_mappings(event.chat_id, message_id)
        logger.info("Deleted Telegram mirrors for Max chat_id=%s message_id=%s", event.chat_id, message_id)


async def on_max_reaction_event(event: MaxReactionEvent) -> None:
    if event.chat_id is None or event.message_id is None:
        return

    message = None
    chat_info = None
    if _max_reaction_info_emoji(event.reaction_info) is None:
        try:
            message = await max_bridge.get_message(event.chat_id, event.message_id)
        except Exception:
            logger.exception(
                "Failed to load Max message for reaction sync chat_id=%s message_id=%s",
                event.chat_id,
                event.message_id,
            )
        try:
            chat_info = await max_bridge.get_chat_info(event.chat_id)
        except Exception:
            logger.exception("Failed to load Max chat info for reaction sync chat_id=%s", event.chat_id)

    await _sync_max_message_state_to_tg(
        event.chat_id,
        event.message_id,
        message,
        event.reaction_info,
        chat_info=chat_info,
        clear_when_empty=True,
    )


async def on_max_event(event: MaxEvent) -> None:
    if isinstance(event, MaxMessageEvent):
        await on_max_message_event(event)
        return
    if isinstance(event, MaxDeleteEvent):
        await on_max_delete_event(event)
        return
    if isinstance(event, MaxReactionEvent):
        await on_max_reaction_event(event)
        return
    if isinstance(event, MaxContactEvent):
        logger.debug("Received Max contact event: %s", event.contact)
        return

async def sync(*, verbose: bool = False):
    if verbose:
        logger.info("--- STARTING FRESH SYNC ---")
    try:
        refresh_user_names_from_login_payload()
        chats = max_bridge.get_login_chats()
        if not chats:
            try:
                chats = await asyncio.wait_for(max_bridge.list_chats(), timeout=20)
            except Exception as exc:
                logger.warning("list_chats failed during sync: %s", exc)
                chats = []
        for c in chats:
            cid = c.get("id")
            if not is_supported_chat(c, cid):
                continue
            try:
                await ensure_user_names_for_chat(c)
                tid = await ensure_chat_topic(cid, chat_info=c, replay_recent_history=False)
                if tid is not None:
                    await max_bridge.ensure_chat_subscription(cid)
            except Exception as exc:
                logger.exception("Sync failed for Max chat_id=%s: %s", cid, exc)
    except Exception as e: logger.error(f"Sync error: {e}")


async def reconcile_chats_forever(interval_seconds: int = CHAT_RECONCILE_INTERVAL_SECONDS):
    while True:
        try:
            await sync()
        except Exception:
            logger.exception("Periodic chat reconciliation failed")
        await asyncio.sleep(interval_seconds)


async def start_max_runtime_forever() -> None:
    while True:
        if await asyncio.to_thread(max_bridge.load_sdk):
            asyncio.create_task(max_bridge.start_polling())
            await max_bridge.wait_ready()
            await sync(verbose=True)
            chat_ids = await load_mapped_chat_ids()
            logger.info("Prepared %s chats for Max subscription", len(chat_ids))
            asyncio.create_task(reconcile_chats_forever())
            asyncio.create_task(sync_recent_message_states_forever())
            asyncio.create_task(backfill_startup_topics_async())
            return

        logger.warning("Max SDK startup failed; retrying in %ss", MAX_STARTUP_RETRY_SECONDS)
        await asyncio.sleep(MAX_STARTUP_RETRY_SECONDS)


async def _record_tg_to_max_mapping(
    tg_message: Message,
    *,
    max_chat_id: int,
    max_message_id: int,
) -> None:
    if not tg_message.message_thread_id or not max_message_id:
        return
    await db.save_message_mapping(
        max_chat_id=max_chat_id,
        max_message_id=max_message_id,
        tg_chat_id=tg_message.chat.id,
        tg_thread_id=tg_message.message_thread_id,
        tg_message_id=tg_message.message_id,
    )


def _extract_tg_edit_text(message: Message) -> str | None:
    if message.text is not None:
        return message.text
    if message.caption is not None:
        return message.caption
    return None

@dp.message(F.chat.id == TG_GROUP_ID)
async def tg_to_max(m: Message):
    if not m.message_thread_id: return
    mcid = await db.get_max_chat_id(m.message_thread_id)
    if mcid is None: return
    
    logger.info(
        "TG->Max: Received message in topic %s (Max CID: %s), has_text=%s has_photo=%s has_video=%s has_voice=%s",
        m.message_thread_id,
        mcid,
        bool(m.text),
        bool(m.photo),
        bool(m.video),
        bool(m.voice),
    )
    
    try:
        # Handle Photo (with or without caption)
        if m.photo:
            logger.info("TG->Max: Processing photo...")
            photo = m.photo[-1]
            tmp = f"data/t_{photo.file_id}.jpg"
            await bot.download(photo, destination=tmp)
            
            caption = m.caption or ""
            sent_message = await max_bridge.send_local_photo(
                chat_id=mcid,
                input_path=tmp,
                text=caption,
            )
            if os.path.exists(tmp): os.remove(tmp)
            sent_message_id = int(sent_message.get("id") or 0) if isinstance(sent_message, dict) else 0
            if sent_message_id:
                remember_bridge_message(sent_message_id)
            confirmed = await confirm_max_message(mcid, sent_message_id)
            if confirmed:
                await _record_tg_to_max_mapping(m, max_chat_id=mcid, max_message_id=sent_message_id)
            logger.info("TG->Max: Photo sent to Max chat_id=%s message_id=%s", mcid, sent_message_id)
        elif m.video:
            logger.info(
                "TG->Max: Processing video file_id=%s file_name=%s mime_type=%s",
                m.video.file_id,
                m.video.file_name,
                m.video.mime_type,
            )
            suffix = ".mp4"
            if m.video.mime_type == "video/quicktime":
                suffix = ".mov"
            elif m.video.mime_type == "video/x-m4v":
                suffix = ".m4v"
            elif m.video.file_name:
                _, ext = os.path.splitext(m.video.file_name)
                if ext:
                    suffix = ext
            tmp = f"data/t_{m.video.file_id}{suffix}"
            try:
                try:
                    await bot.download(m.video, destination=tmp)
                except TelegramBadRequest as exc:
                    if "file is too big" in str(exc).lower():
                        size_mb = (m.video.file_size or 0) / (1024 * 1024)
                        logger.warning(
                            "TG->Max: Video is too large for Telegram Bot API download. "
                            "file_id=%s file_size=%s bytes",
                            m.video.file_id,
                            m.video.file_size,
                        )
                        await bot.send_message(
                            TG_GROUP_ID,
                            message_thread_id=m.message_thread_id,
                            text=(
                                "Видео не отправлено в Max: Telegram Bot API не даёт боту скачать "
                                f"этот файл из-за размера ({size_mb:.1f} MB)."
                            ),
                        )
                        return
                    raise
                caption = m.caption or ""
                sent_message = await max_bridge.send_local_video(
                    chat_id=mcid,
                    input_path=tmp,
                    text=caption,
                )
                sent_message_id = int(sent_message.get("id") or 0) if isinstance(sent_message, dict) else 0
                if sent_message_id:
                    remember_bridge_message(sent_message_id)
                confirmed = await confirm_max_message(mcid, sent_message_id)
                if confirmed:
                    await _record_tg_to_max_mapping(m, max_chat_id=mcid, max_message_id=sent_message_id)
                    logger.info(
                        "TG->Max: Video sent to Max chat_id=%s file_id=%s message_id=%s",
                        mcid,
                        m.video.file_id,
                        sent_message_id,
                    )
                else:
                    logger.error(
                        "TG->Max: Max did not confirm video message chat_id=%s file_id=%s message_id=%s",
                        mcid,
                        m.video.file_id,
                        sent_message_id,
                    )
                    await bot.send_message(
                        TG_GROUP_ID,
                        message_thread_id=m.message_thread_id,
                        text="Видео не подтверждено в Max после отправки. Нужна дополнительная проверка протокола.",
                    )
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
        elif m.voice:
            logger.info(
                "TG->Max: Processing voice file_id=%s duration=%s mime_type=%s",
                m.voice.file_id,
                m.voice.duration,
                m.voice.mime_type,
            )
            tmp = voice_temp_path(m.voice.file_id)
            try:
                await bot.download(m.voice, destination=tmp)
                sent_message = await max_bridge.send_local_audio(
                    chat_id=mcid,
                    input_path=tmp,
                    duration=m.voice.duration or 0,
                    telegram_waveform=m.voice.waveform,
                )
                sent_message_id = int(sent_message.get("id") or 0) if isinstance(sent_message, dict) else 0
                if sent_message_id:
                    remember_bridge_message(sent_message_id)
                confirmed = await confirm_max_message(mcid, sent_message_id)
                if confirmed:
                    await _record_tg_to_max_mapping(m, max_chat_id=mcid, max_message_id=sent_message_id)
                    logger.info(
                        "TG->Max: Voice sent to Max chat_id=%s file_id=%s message_id=%s",
                        mcid,
                        m.voice.file_id,
                        sent_message_id,
                    )
                else:
                    logger.error(
                        "TG->Max: Max did not confirm voice message chat_id=%s file_id=%s message_id=%s",
                        mcid,
                        m.voice.file_id,
                        sent_message_id,
                    )
                    await bot.send_message(
                        TG_GROUP_ID,
                        message_thread_id=m.message_thread_id,
                        text="Голосовое не подтверждено в Max после отправки. Нужна дополнительная проверка протокола.",
                    )
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
        elif m.document:
            logger.info(
                "TG->Max: Processing document file_id=%s file_name=%s mime_type=%s",
                m.document.file_id,
                m.document.file_name,
                m.document.mime_type,
            )
            suffix = ""
            if m.document.file_name:
                _, ext = os.path.splitext(m.document.file_name)
                suffix = ext
            tmp = f"data/t_{m.document.file_id}{suffix}"
            try:
                await bot.download(m.document, destination=tmp)
                caption = m.caption or ""
                sent_message = await max_bridge.send_local_file(
                    chat_id=mcid,
                    input_path=tmp,
                    text=caption,
                )
                sent_message_id = int(sent_message.get("id") or 0) if isinstance(sent_message, dict) else 0
                if sent_message_id:
                    remember_bridge_message(sent_message_id)
                confirmed = await confirm_max_message(mcid, sent_message_id)
                if confirmed:
                    await _record_tg_to_max_mapping(m, max_chat_id=mcid, max_message_id=sent_message_id)
                logger.info("TG->Max: Document sent to Max chat_id=%s message_id=%s", mcid, sent_message_id)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
        # Handle Text (only if NO photo, to avoid double sending if there is a caption)
        elif m.text:
            sent_message = await max_bridge.send_text(mcid, m.text)
            sent_message_id = int(sent_message.get("id") or 0) if isinstance(sent_message, dict) else 0
            if sent_message_id:
                remember_bridge_message(sent_message_id)
            confirmed = await confirm_max_message(mcid, sent_message_id)
            if confirmed:
                await _record_tg_to_max_mapping(m, max_chat_id=mcid, max_message_id=sent_message_id)
            logger.info("TG->Max: Text sent to Max chat_id=%s message_id=%s text=%s...", mcid, sent_message_id, m.text[:30])
    except Exception as e:
        logger.exception(f"TG->Max error: {e}")


@dp.edited_message(F.chat.id == TG_GROUP_ID)
async def tg_edit_to_max(m: Message):
    if is_recent_tg_reaction_update(m.chat.id, m.message_id):
        logger.info("Skipping Telegram edit update caused by bot reaction tg_message_id=%s", m.message_id)
        return
    if m.from_user and m.from_user.is_bot:
        logger.info("Skipping Telegram edit update from bot message tg_message_id=%s", m.message_id)
        return
    mapping = await db.get_max_message_for_tg_message(m.chat.id, m.message_id)
    if not mapping:
        return
    new_text = _extract_tg_edit_text(m)
    if new_text is None and not (m.photo or m.video or m.document):
        return
    if new_text is not None:
        try:
            current_message = await max_bridge.get_message(mapping["max_chat_id"], mapping["max_message_id"])
            if isinstance(current_message, dict) and (current_message.get("text") or "") == new_text:
                logger.info("Skipping Telegram edit update with unchanged Max text tg_message_id=%s", m.message_id)
                return
        except Exception:
            logger.exception("Failed to compare Telegram edit with Max message tg_message_id=%s", m.message_id)
    try:
        await max_bridge.edit_message(
            chat_id=mapping["max_chat_id"],
            message_id=mapping["max_message_id"],
            text=new_text or "",
            edit_attaches=False,
        )
        logger.info(
            "TG->Max: Edited message propagated tg_message_id=%s -> max_message_id=%s",
            m.message_id,
            mapping["max_message_id"],
        )
    except Exception as exc:
        logger.exception("TG->Max edit propagation failed for tg_message_id=%s: %s", m.message_id, exc)

async def main():
    await db.init()
    max_bridge.set_on_event(on_max_event)
    await start_max_runtime_forever()
    logger.info("MAX runtime ready, starting Telegram polling...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(
        bot,
        polling_timeout=TG_POLLING_TIMEOUT,
        allowed_updates=dp.resolve_used_update_types(),
    )

if __name__ == "__main__":
    ensure_single_instance()
    try:
        asyncio.run(main())
    finally:
        cleanup_pid_file()
