import os
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()

# Telegram
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
_tg_group_id_raw = os.getenv("TG_GROUP_ID", "0").strip()
# Remove any accidental double dashes and convert to int
# If starts with -, keep one, then remove others
if _tg_group_id_raw.startswith("-"):
    _clean_id = "-" + _tg_group_id_raw[1:].replace("-", "")
else:
    _clean_id = _tg_group_id_raw.replace("-", "")
TG_GROUP_ID = int(_clean_id)



# Path for data
DATA_DIR = Path("data")
AUTH_BUNDLE_PATH = DATA_DIR / "auth_bundle.json"
DB_PATH = str(DATA_DIR / "bridge.db")

# Device configuration (can be hardcoded or randomized)
MAX_DEVICE_ID = os.getenv("MAX_DEVICE_ID", "max2tg-bridge-default")
TG_POLLING_TIMEOUT = int(os.getenv("TG_POLLING_TIMEOUT", "30"))
CHAT_RECONCILE_INTERVAL_SECONDS = int(os.getenv("CHAT_RECONCILE_INTERVAL_SECONDS", "300"))
