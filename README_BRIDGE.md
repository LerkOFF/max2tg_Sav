# Bridge Notes

## Как bridge находит MaxAPI

Bridge не хранит `max_proto` внутри этого репозитория.

Поиск внешнего checkout идёт в таком порядке:
1. `MAXAPI_REPO_PATH`
2. `vendor/MaxAPI`
3. `external/MaxAPI`
4. `../MaxAPI`
5. `../maxapi`

Логика лежит в [maxapi_bootstrap.py](maxapi_bootstrap.py).

В Docker это упрощено:
- образ кладёт `vendor/MaxAPI` в `/opt/maxapi`
- `MAXAPI_REPO_PATH=/opt/maxapi` выставляется в `Dockerfile`
- внешний checkout на хосте контейнеру не нужен

## Runtime-архитектура

В контейнере bridge работает в 3 процессах:
- `main.py` — Telegram polling, SQLite, маршрутизация событий и topic/message mapping
- `max-sdk-worker` — все Max SDK операции `send_*`, `edit_message`, `get_message`, `get_chat_history`, `download_*`
- `max-polling` — подписки на MAX чаты и чтение входящих `MaxEvent`

Причина такой схемы практическая: GOST/LibreSSL стек внутри MaxAPI может падать `SIGSEGV` в долгоживущем процессе при параллельных сетевых операциях. Вынос polling и SDK-call path в отдельные worker processes изолирует эти сбои от Telegram runtime.

## Какие файлы относятся именно к bridge

- [main.py](main.py)
- [max_bridge.py](max_bridge.py)
- [database.py](database.py)
- [rebuild_topics.py](rebuild_topics.py)
- [config.py](config.py)
- [init_max.py](init_max.py)
- [complete_max_auth.py](complete_max_auth.py)
- [check_chats.py](check_chats.py)
- [scripts/start_bot.sh](scripts/start_bot.sh)
- [scripts/stop_bot.sh](scripts/stop_bot.sh)

## Локальные runtime-данные

Не коммитятся:
- `.env`
- `data/auth_bundle.json`
- `data/bridge.db`
- `data/user_names.json`
- `logs/`
- `vendor/`

## Что делает bridge поверх MaxAPI

- создаёт Telegram topic на каждый чат MAX
- хранит `chat_mapping`
- хранит `message_mapping`
- подавляет эхо `TG -> Max -> TG`
- резолвит названия dialog-топиков из `login_payload["contacts"]` и `participants`, а не из `Чат <id>`
- делает controlled replay истории при создании новых topics
- умеет пересобрать topics через [rebuild_topics.py](rebuild_topics.py)
- пробрасывает edit/delete/file workflows между системами

## Известные edge cases

- MAX-сообщения со служебными attach (`INLINE_KEYBOARD`, `CONTROL`) нельзя учитывать как отдельное медиа при upsert в Telegram. Для idempotent update bridge должен редактировать caption у одного `PHOTO/VIDEO/FILE`, а не делать delete+resend из-за второго служебного attach.
- Повторные `opcode=128` для одного `max_message_id` возможны. Защита от дублей держится на `message_mapping` и idempotent update, а не на предположении “event всегда придёт один раз”.

## Ограничения

- реакции пока не включены
- удаление поддержано только `Max -> TG`
- большие Telegram-видео по-прежнему упираются в лимит Bot API на скачивание файла
