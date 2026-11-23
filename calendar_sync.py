"""Google Calendar connection through Recall Calendar V2 (app-managed scheduling).

The app runs the Google OAuth flow and hands the refresh token to Recall. The app keeps only the
Recall calendar id, never the Google token.
"""
import json
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import config

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
STATE_TTL_S = 600

_pending_states: dict[str, float] = {}


def redirect_uri() -> str:
    return f"{config.PUBLIC_URL}/calendar/google/callback"


def auth_url() -> str:
    state = secrets.token_urlsafe(24)
    _pending_states[state] = time.time()
    params = {
        "client_id": config.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",  # we need a refresh token for Recall
        "prompt": "consent",  # Google only returns a refresh token on consent
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def consume_state(state: str) -> bool:
    """One-time state check against CSRF on the callback."""
    created = _pending_states.pop(state, None)
    return created is not None and time.time() - created < STATE_TTL_S


async def exchange_code(code: str) -> str:
    import httpx

    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.post(GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": config.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": config.GOOGLE_OAUTH_CLIENT_SECRET,
            "redirect_uri": redirect_uri(),
            "grant_type": "authorization_code",
        })
        r.raise_for_status()
        token = r.json().get("refresh_token")
    if not token:
        raise ValueError("Google did not return a refresh_token; connect again")
    return token


def recall_calendar_body(refresh_token: str) -> dict:
    return {
        "oauth_client_id": config.GOOGLE_OAUTH_CLIENT_ID,
        "oauth_client_secret": config.GOOGLE_OAUTH_CLIENT_SECRET,
        "oauth_refresh_token": refresh_token,
        "platform": "google_calendar",
    }


async def create_recall_calendar(refresh_token: str) -> str:
    import httpx
    import recall

    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(f"{recall.base_v2()}/calendars/", headers=recall.headers(),
                            json=recall_calendar_body(refresh_token))
        r.raise_for_status()
        return r.json()["id"]


def _db_path() -> Path:
    return Path(config.CALENDAR_DB)


def load_calendars() -> list[dict]:
    path = _db_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def save_calendar(calendar_id: str) -> None:
    calendars = load_calendars()
    calendars.append({"platform": "google_calendar", "calendar_id": calendar_id, "connected_at": time.time()})
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(calendars, indent=2), encoding="utf-8")
