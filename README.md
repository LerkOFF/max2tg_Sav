# Max2TG

Two-way bridge between MAX and Telegram forum topics.

This fork uses `maxapi-python` (`pymax`) from PyPI. It does not need a local
`MaxAPI` checkout, `max_proto`, or a custom LibreSSL/GOST Python build.

## Setup

Install dependencies:

```bash
pip install -r requirements.txt
```

Create `.env`:

```env
TG_BOT_TOKEN=...
TG_GROUP_ID=-100...
MAX_PHONE=+79990000000
MAX_DEVICE_ID=max2tg-bridge
MAX_SESSION_DIR=pymax
MAX_SESSION_NAME=session.db
```

Authorize MAX once from an interactive terminal:

```bash
python init_max.py
```

The script asks for the SMS code and stores the session in
`data/pymax/session.db`.

Run locally:

```bash
python main.py
```

## Docker

Build and run:

```bash
docker compose build
docker compose up -d
```

The compose file mounts:

- `.env` to `/app/.env`
- `data/` to `/app/data`
- `logs/` to `/app/logs`

Authorize MAX before starting the container, or run `python init_max.py` inside
an interactive container with the same mounted `data/` directory.

## Files

- `main.py` - Telegram bot, topic routing, message mapping.
- `max_bridge.py` - compatibility adapter over `pymax.Client`.
- `database.py` - SQLite mappings.
- `init_max.py` - one-step MAX SMS authorization.
- `check_chats.py` - prints visible MAX chats.
- `README_BRIDGE.md` - runtime details and limits.

## Runtime Data

Do not commit:

- `.env`
- `data/pymax/`
- `data/bridge.db`
- `data/user_names.json`
- `logs/`
- `vendor/`
