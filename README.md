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

Run locally:

```bash
python main.py
```

On first local run, if `data/pymax/session.db` is missing, authorize MAX with:

```bash
python init_max.py
```

## Docker

One-time setup:

```bash
cp .env.example .env
```

Fill in `.env`, then start everything with one command:

```bash
docker compose up --build
```

On the first start the container will:

1. validate `.env`
2. request MAX SMS authorization if `data/pymax/session.db` is missing
3. start the bridge automatically after auth

Ways to enter the SMS code on first start:

- enter it in the terminal when running `docker compose up`
- write the code to `data/.max_sms_code` while the container is waiting
- set `MAX_SMS_CODE=123456` in `.env` and restart the container

After the first successful auth, the same command also works in the background:

```bash
docker compose up -d --build
```

The compose file mounts:

- `.env` to `/app/.env`
- `data/` to `/app/data`
- `logs/` to `/app/logs`

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
