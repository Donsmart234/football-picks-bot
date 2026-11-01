"""Runtime configuration, read from environment variables only. Never log these values."""
import os
from pathlib import Path


def _int(name: str, default: int | None = None) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "").strip()
OWNER_TELEGRAM_ID = _int("OWNER_TELEGRAM_ID")

# Webhook mode is used when WEBHOOK_URL (or Render's RENDER_EXTERNAL_URL) is set.
WEBHOOK_URL = (os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
PORT = _int("PORT")
# Public https base for the Mini App (<base>/app). Defaults to the webhook / Render URL.
PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL") or WEBHOOK_URL).strip().rstrip("/")

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent / "data"))
DB_PATH = DATA_DIR / "botdata.sqlite3"

API_FOOTBALL_DAILY_LIMIT = _int("API_FOOTBALL_DAILY_LIMIT", 90)
LOCAL_TZ = os.environ.get("BOT_TIMEZONE", "Africa/Lagos")
SPORTYBET_CACHE_SECONDS = _int("SPORTYBET_CACHE_SECONDS", 300)

# Relay for SportyBet calls (Render's IP gets an AWS WAF challenge). Set to an empty string for direct mode.
SPORTYBET_RELAY_URL = os.environ.get("SPORTYBET_RELAY_URL",
                                     "https://ectglmhgqfiipexvczmh.supabase.co/functions/v1/sb").strip()

# Bet analyzer access: "owner" (default: the bot stays private), "allowlist" (owner + ANALYZER_USERS) or "public".
# Only /analyze, bare codes, the analyzer buttons and the Mini App's /api/analyze* routes open up; everything else
# stays owner-only, and only the owner's analyses are saved to the history.
ANALYZER_ACCESS = os.environ.get("ANALYZER_ACCESS", "owner").strip().lower()
ANALYZER_USERS = {int(x) for x in os.environ.get("ANALYZER_USERS", "").replace(" ", "").split(",") if x.isdigit()}


def analyzer_allowed(user_id: int | None, owner_id: int | None) -> bool:
    if user_id is None:
        return False
    if owner_id is not None and int(user_id) == int(owner_id):
        return True
    if ANALYZER_ACCESS == "public":
        return True
    if ANALYZER_ACCESS == "allowlist":
        return int(user_id) in ANALYZER_USERS
    return False
