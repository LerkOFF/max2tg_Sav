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
2. start Telegram polling
3. connect to MAX; if the session is missing or expired, ask for an SMS code
   in the group's General topic (`/1`)
4. if MAX requests a second-factor password, ask for it in a private chat
   with the same Telegram bot

Reply in General with only the digits, for example `123456`. The file
`data/.max_sms_code` still works as a fallback.
If the account has a password, open a private chat with the bot, press Start,
and send the password there. The bot deletes the received password message
after handing it to MAX. If the SMS code came from Telegram, only that sender
can provide the password; if it came from the file, a group administrator can.

Daily, the bot probes the MAX session. If the token is dead, it requests a
new SMS and again waits for the code in General `/1`.
If no code arrives, the bot requests a fresh SMS every 24 hours and posts a
new notice in General. The request time is stored in `data/`, so a container
restart does not trigger an extra SMS before the next daily request.

Important:

- use a **fresh** SMS code for each login attempt
- do **not** keep `MAX_SMS_CODE` in `.env` — stale values break auth on restart
- if the code is wrong, the container waits for a new one instead of exiting

Other options:

- enter the code in the terminal when running `docker compose up`

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
