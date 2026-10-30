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

# --- Claude ----------------------------------------------------------------
# ANTHROPIC_API_KEY is read by the SDK directly.
ACTION_MODEL = os.getenv("ACTION_MODEL", "claude-sonnet-5-5")  # detects + executes actions
FOCUS_MODEL = os.getenv("FOCUS_MODEL", "claude-haiku-5-5")  # cheap, frequent focus checks
MCP_BETA = os.getenv("MCP_BETA", "mcp-client-2025-11-20")
MCP_SERVERS_FILE = os.getenv("MCP_SERVERS_FILE", "mcp_servers.json")

# --- Behaviour -------------------------------------------------------------
AUTO_EXECUTE = _bool("AUTO_EXECUTE", False)  # False = ask in Meet chat before acting
DRY_RUN = _bool("DRY_RUN", False)  # True = print chat messages instead of calling Recall
DETECT_INTERVAL_S = int(os.getenv("DETECT_INTERVAL_S", "15"))
FOCUS_INTERVAL_S = int(os.getenv("FOCUS_INTERVAL_S", "45"))
WINDOW_S = int(os.getenv("WINDOW_S", "150"))  # rolling window used for focus checks
MIN_WINDOW_WORDS = int(os.getenv("MIN_WINDOW_WORDS", "40"))  # skip focus checks on silence
DRIFT_STRIKES = int(os.getenv("DRIFT_STRIKES", "2"))  # consecutive off-topic checks before warning
WARN_COOLDOWN_S = int(os.getenv("WARN_COOLDOWN_S", "180"))
METRICS_DIR = os.getenv("METRICS_DIR", "metrics")  # meetings.jsonl is appended here
CHAT_PREFIX = "🤖"  # every bot message starts with this, so we can ignore our own echoes


def load_mcp_servers() -> list[dict]:
    """Load MCP server definitions; ${ENV_VARS} inside the file are expanded."""
    path = Path(MCP_SERVERS_FILE)
    if not path.exists():
        return []
    servers = json.loads(os.path.expandvars(path.read_text()))
    # Drop servers whose token placeholder was never filled in.
    return [s for s in servers if "${" not in json.dumps(s)]
