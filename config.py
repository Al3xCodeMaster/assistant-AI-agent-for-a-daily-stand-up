"""Settings loaded from environment variables (.env supported)."""
import json
import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is optional
    pass


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


# --- Recall.ai -------------------------------------------------------------
RECALL_API_KEY = os.getenv("RECALL_API_KEY", "")
RECALL_REGION = os.getenv("RECALL_REGION", "us-west-2")  # us-east-1 | us-west-2 | eu-central-1 | ap-northeast-1
RECALL_VERIFICATION_SECRET = os.getenv("RECALL_VERIFICATION_SECRET", "")  # whsec_...
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")  # e.g. https://my-static.ngrok-free.app
BOT_NAME = os.getenv("BOT_NAME", "Meeting Copilot")
TRANSCRIPT_LANGUAGE = os.getenv("TRANSCRIPT_LANGUAGE", "en")  # "es" for Spanish meetings
# meeting_captions = Google Meet's own captions (no Recall transcription charge); recallai = Recall's engine
TRANSCRIPT_PROVIDER = os.getenv("TRANSCRIPT_PROVIDER", "meeting_captions")
# Team members the detector may invite or assign (name, login, email, openproject_id). Local data, not in git
OPENPROJECT_USERS_FILE = os.getenv("OPENPROJECT_USERS_FILE", "data/openproject_users.json")

# --- Gemini ----------------------------------------------------------------
# GEMINI_API_KEY is read by the google-genai SDK directly (llm.py).
# Model ids: check the ones your key can use in Google AI Studio; set them here without code changes.
ACTION_MODEL = os.getenv("ACTION_MODEL", "gemini-3.5-flash-lite")  # detects + executes actions
FOCUS_MODEL = os.getenv("FOCUS_MODEL", "gemini-3.5-flash-lite")  # cheap, frequent focus checks
# Time zone of the team: the model resolves "mañana", "el viernes" and calendar times with it.
TIMEZONE = os.getenv("TIMEZONE", "America/Bogota")
MCP_SERVERS_FILE = os.getenv("MCP_SERVERS_FILE", "mcp_servers.json")

# --- Behaviour -------------------------------------------------------------
AUTO_EXECUTE = _bool("AUTO_EXECUTE", False)  # False = ask in Meet chat before acting
# Only OpenProject status changes (kind=status_change) run without 'ok N'. Everything else still waits for approval
AUTO_EXECUTE_STATUS = _bool("AUTO_EXECUTE_STATUS", False)
# People whose spoken approval ("aprueba la propuesta uno" / "ok 1") runs a pending proposal. Names as Meet shows them
VOICE_APPROVERS = [n.strip() for n in os.getenv("VOICE_APPROVERS", "Diego Alcides Carabali").split(",") if n.strip()]
DRY_RUN = _bool("DRY_RUN", False)  # True = print chat messages instead of calling Recall
DETECT_INTERVAL_S = int(os.getenv("DETECT_INTERVAL_S", "15"))
FOCUS_INTERVAL_S = int(os.getenv("FOCUS_INTERVAL_S", "45"))
WINDOW_S = int(os.getenv("WINDOW_S", "150"))  # rolling window used for focus checks
MIN_WINDOW_WORDS = int(os.getenv("MIN_WINDOW_WORDS", "40"))  # skip focus checks on silence
DAILY_TOTAL_S = int(os.getenv("DAILY_TOTAL_S", "900"))  # talk-time budget for a daily (mode=daily)
DAILY_MIN_TURN_S = int(os.getenv("DAILY_MIN_TURN_S", "30"))  # per-person floor when many attendees
PROJECT_INTERVAL_S = int(os.getenv("PROJECT_INTERVAL_S", "60"))  # how often the project in discussion is re-labeled
DRIFT_STRIKES = int(os.getenv("DRIFT_STRIKES", "2"))  # consecutive off-topic checks before warning
WARN_COOLDOWN_S = int(os.getenv("WARN_COOLDOWN_S", "180"))
METRICS_DIR = os.getenv("METRICS_DIR", "metrics")  # meetings.jsonl is appended here
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")  # Web application client from Google Cloud
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
CALENDAR_DB = os.getenv("CALENDAR_DB", "data/calendars.json")  # Recall calendar ids only, no Google tokens
NOTIFY_WEBHOOK_URL = os.getenv("NOTIFY_WEBHOOK_URL", "")  # Slack or Google Chat incoming webhook (optional)
# --- OpenProject (daily mode): blocked HU are read at the start; results go back at the end
OPENPROJECT_PROJECT_ID = os.getenv("OPENPROJECT_PROJECT_ID", "")  # project id or identifier, e.g. "sirap"
OPENPROJECT_DAILY_WP_ID = os.getenv("OPENPROJECT_DAILY_WP_ID", "")  # work package that receives the daily metrics
# --- Google Meet notes ("Notas de Gemini" docs in Drive): wait for Gemini to write them after bot.done
GOOGLE_REFRESH_TOKEN_NOTES = os.getenv("GOOGLE_REFRESH_TOKEN_NOTES", "")  # needs drive.readonly (re-consent)
MEET_NOTES_WAIT_S = int(os.getenv("MEET_NOTES_WAIT_S", "600"))
MEET_NOTES_POLL_S = int(os.getenv("MEET_NOTES_POLL_S", "60"))
CHAT_PREFIX = "🤖"  # every bot message starts with this, so we can ignore our own echoes


def load_mcp_servers() -> list[dict]:
    """Load MCP server definitions; ${ENV_VARS} inside the file are expanded."""
    path = Path(MCP_SERVERS_FILE)
    if not path.exists():
        return []
    servers = json.loads(os.path.expandvars(path.read_text()))
    # Drop servers whose token placeholder was never filled in.
    return [s for s in servers if "${" not in json.dumps(s)]
