# Max2TG

`Max2TG` — это двусторонний bridge между `MAX` и `Telegram Topics`.

В этом репозитории хранятся только bridge-специфичные файлы:
- Telegram bot runtime
- маппинг `Max chat <-> Telegram topic`
- маппинг `Max message <-> Telegram message`
- логика echo suppression, replay/rebuild topics, edit/delete propagation
- процессная изоляция Max polling и Max SDK RPC от Telegram runtime
- локальные скрипты запуска и авторизации

Протокольное ядро `MAX` сюда больше не вендорится.

Есть 2 режима:
- локальный запуск: bridge использует внешний checkout `MaxAPI`
- Docker: образ сам клонирует `MaxAPI` на этапе сборки

## Зависимость от MaxAPI

Нужен отдельный клон репозитория `MaxAPI`.

Рекомендуемый вариант:

```bash
cd ..
git clone https://github.com/talifan/MaxAPI.git
```

Тогда структура будет такой:

```text
.../LLM_Activities/
  max2tg/
  MaxAPI/
```

Поддерживаются и другие варианты:
- положить `MaxAPI` в `vendor/MaxAPI` внутри этого репозитория
- положить `MaxAPI` в `external/MaxAPI`
- задать `MAXAPI_REPO_PATH=/полный/путь/к/MaxAPI`

Bridge автоматически ищет `MaxAPI` в этих местах через [maxapi_bootstrap.py](maxapi_bootstrap.py).

## Что должно лежать в MaxAPI

Нужен именно checkout репозитория `MaxAPI`, потому что bridge использует его:
- `max_proto/`
- `max_fresh_auth.py`
- `max_sdk_cli.py`

Минимально для runtime нужен `max_proto/`, но проще и надёжнее держать полный клон `MaxAPI`.

## Установка

1. Поставить Python-зависимости bridge:

```bash
pip install aiogram aiosqlite python-dotenv
```

2. Поставить зависимости `MaxAPI` в его checkout:

```bash
cd ../MaxAPI
pip install lz4 msgpack
```

3. Вернуться в `max2tg` и создать `.env`:

```env
TG_BOT_TOKEN=...
TG_GROUP_ID=-100...
MAX_DEVICE_ID=+79...
```

4. Пройти авторизацию MAX:

```bash
python3 init_max.py
python3 complete_max_auth.py 123456
```

Bundle авторизации будет сохранён в `data/auth_bundle.json`.

## Docker

В Docker внешний checkout `../MaxAPI` на хосте не нужен.

Текущий `docker-compose.yml` монтирует секреты и runtime-данные с хоста:
- `.env` -> `/app/.env:ro`
- `data/` -> `/app/data`
- `logs/` -> `/app/logs`

`Dockerfile` собирает отдельный Python с `LibreSSL 2.8.3` для GOST TLS и берёт `MaxAPI` из `vendor/MaxAPI` внутри build context:
- копирует `vendor/MaxAPI` в `/opt/maxapi`
- выставляет `MAXAPI_REPO_PATH=/opt/maxapi`
- устанавливает зависимости bridge и `MaxAPI`

Сборка:

```bash
docker compose build
```

Запуск:

```bash
docker compose up -d
```

Логи:

```bash
tail -f logs/bridge.log
```

По умолчанию `docker-compose.yml` тянет:
- `MAXAPI_REPO=https://github.com/talifan/MaxAPI.git`
- `MAXAPI_REF=main`

Если нужно собрать образ на другом ref:

```bash
docker compose build \
  --build-arg MAXAPI_REF=<branch-or-tag>
```

## Запуск

```bash
./scripts/start_bot.sh
```

Остановка:

```bash
./scripts/stop_bot.sh
```

## Структура этого репозитория

- [main.py](main.py) — основной bridge и Telegram handlers
- [max_bridge.py](max_bridge.py) — адаптер над внешним `MaxAPI`
- [database.py](database.py) — SQLite mapping для чатов и сообщений
- [rebuild_topics.py](rebuild_topics.py) — пересборка Telegram topics и controlled replay
- [init_max.py](init_max.py) — запрос кода авторизации через внешний `MaxAPI`
- [complete_max_auth.py](complete_max_auth.py) — завершение авторизации и сохранение bundle
- [README_BRIDGE.md](README_BRIDGE.md) — подробная bridge-документация
- [TODO.md](TODO.md) — текущий backlog

## Что умеет bridge

- `Max -> TG`: текст, фото, видео, файлы
- `TG -> Max`: текст, фото, видео, документы
- отдельные Telegram topics для чатов MAX
- persistent message mapping
- suppression эха
- `Max -> TG` delete propagation
- edit propagation:
  - `TG -> Max`
  - `Max -> TG`
- устойчивый runtime в Docker:
  - основной процесс `main.py` обслуживает Telegram и SQLite
  - `max-sdk-worker` выполняет все SDK send/download/history вызовы в отдельном process
  - `max-polling` слушает MAX events в отдельном process

Реакции пока сознательно не включены в runtime.

## Проверка MaxAPI bootstrap

Быстрая проверка, что bridge видит внешний `MaxAPI`:

```bash
python3 - <<'PY'
from maxapi_bootstrap import bootstrap_maxapi
print(bootstrap_maxapi())
PY
```

Если всё в порядке, вы увидите путь к checkout `MaxAPI` либо `None`, если `max_proto` уже доступен в `PYTHONPATH`.
