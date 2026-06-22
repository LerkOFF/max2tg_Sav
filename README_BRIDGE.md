# Bridge Notes

## MAX runtime

This fork uses the PyPI package `maxapi-python` (`pymax`). A local `MaxAPI`
checkout, `max_proto`, LibreSSL/GOST build, and `MAXAPI_REPO_PATH` are no longer
required.

Authorization is stored by `pymax` as SQLite session data:

- `data/pymax/session.db` by default
- configurable through `MAX_SESSION_DIR` and `MAX_SESSION_NAME`

Run initial authorization from an interactive terminal:

```bash
python init_max.py
```

The script asks for the SMS code and then writes the session file. After that the
bot can run non-interactively through `python main.py` or Docker Compose.

## Environment

Required:

- `TG_BOT_TOKEN`
- `TG_GROUP_ID`
- `MAX_PHONE`

Optional:

- `MAX_DEVICE_ID=max2tg-bridge`
- `MAX_SESSION_DIR=pymax`
- `MAX_SESSION_NAME=session.db`
- `STARTUP_BACKFILL_ENABLED=true` — backfill recent history on startup
- `STARTUP_BACKFILL_CHATS_LIMIT=10` — only the N most recent MAX dialogs
- `STARTUP_BACKFILL_MESSAGES_PER_CHAT=10` — messages per dialog on startup

## Runtime architecture

- `main.py` handles Telegram polling, SQLite mapping, topic creation, message
  upserts, edits, deletes, and reactions.
- `max_bridge.py` wraps `pymax.Client` and preserves the old `MaxBridge`
  interface used by `main.py`.
- `database.py` stores Telegram topic mappings and message mappings.

## Local runtime data

Do not commit:

- `.env`
- `data/pymax/`
- `data/bridge.db`
- `data/user_names.json`
- `logs/`
- `vendor/`

## Current behavior

- Creates one Telegram forum topic per MAX chat/dialog/channel.
- Mirrors MAX -> Telegram messages, media, edits, deletes, and reactions where
  the underlying `pymax` event/API exposes the data.
- Mirrors Telegram topic messages back to the mapped MAX chat, including voice
  messages as native MAX `AUDIO` attachments (not generic files).
- Suppresses echo messages from Telegram -> MAX -> Telegram using message IDs.
- On startup: creates topics for all MAX chats, then backfills only the most
  recent dialogs in the background (defaults: 10 chats x 10 messages).
- After startup: only live messages are mirrored; new MAX chats get a topic
  without history replay.

## Known limits

- TG -> MAX voice uses `FILE_UPLOAD` (opcode 87), then `MSG_TYPING` / `UPLOAD_ATTACH_PREP`
  (opcode 65) with type `AUDIO` or `FILE`, multipart HTTP upload, and `NOTIF_ATTACH` wait.
  Native `AUDIO` attach (`audioId`/`token`, `duration`, `wave`) is tried first; on
  `Invalid attachment` the bridge falls back to a `FILE` attach.
- History pagination is adapted to `pymax` history API and may need tuning after
  live testing on real chats.
- Media download URL availability depends on what MAX returns for each
  attachment type.
- Large Telegram videos are still limited by Telegram Bot API download limits.
