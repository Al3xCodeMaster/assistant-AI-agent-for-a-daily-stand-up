"""Google Meet notes written by Gemini ("Notas de Gemini" Google Docs in Drive).

Plain httpx, same OAuth style as gcal_mcp. Needs a refresh token with the drive.readonly scope
(GOOGLE_REFRESH_TOKEN_NOTES). Gemini writes the doc a few minutes after the meeting ends, so the
caller polls with `wait_for_notes`.
"""
import asyncio
import datetime as dt
import logging

import httpx

log = logging.getLogger("meet_notes")

TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_API = "https://www.googleapis.com/drive/v3"
GEMINI_NAMES = ("Notas de Gemini", "Notes by Gemini")  # Spanish and English Workspace titles


async def _access_token(http: httpx.AsyncClient, client_id: str, client_secret: str, refresh_token: str) -> str:
    r = await http.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })
    r.raise_for_status()
    return r.json()["access_token"]


def pick_doc(files: list[dict], meeting_title: str) -> dict | None:
    """Prefer the doc whose name holds the meeting title; otherwise the most recently modified one."""
    if not files:
        return None
    title = (meeting_title or "").strip().lower()
    if title:
        for f in files:  # files arrive newest first
            if title in f.get("name", "").lower():
                return f
    return files[0]


async def find_notes(client_id: str, client_secret: str, refresh_token: str,
                     meeting_start: dt.datetime, meeting_title: str = "",
                     http: httpx.AsyncClient | None = None) -> str:
    """Plain text of the Gemini notes doc written after meeting_start. "" when none exists yet."""
    if not refresh_token:
        return ""
    own = http is None
    http = http or httpx.AsyncClient(timeout=30)
    try:
        token = await _access_token(http, client_id, client_secret, refresh_token)
        headers = {"Authorization": f"Bearer {token}"}
        since = meeting_start.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        names = " or ".join(f"name contains '{n}'" for n in GEMINI_NAMES)
        query = (f"mimeType='application/vnd.google-apps.document' and ({names}) "
                 f"and modifiedTime > '{since}' and trashed = false")
        r = await http.get(f"{DRIVE_API}/files", headers=headers, params={
            "q": query, "orderBy": "modifiedTime desc", "fields": "files(id,name,modifiedTime)", "pageSize": "20",
        })
        r.raise_for_status()
        doc = pick_doc(r.json().get("files", []), meeting_title)
        if not doc:
            return ""
        exp = await http.get(f"{DRIVE_API}/files/{doc['id']}/export", headers=headers,
                             params={"mimeType": "text/plain"})
        exp.raise_for_status()
        return exp.text.strip()
    finally:
        if own:
            await http.aclose()


async def wait_for_notes(client_id: str, client_secret: str, refresh_token: str,
                         meeting_start: dt.datetime, meeting_title: str,
                         wait_s: int, poll_s: int, http: httpx.AsyncClient | None = None) -> str:
    """Poll Drive until the notes appear or wait_s passes. Errors are logged and end the wait."""
    deadline = asyncio.get_running_loop().time() + wait_s
    while True:
        try:
            text = await find_notes(client_id, client_secret, refresh_token, meeting_start, meeting_title, http)
        except Exception:  # noqa: BLE001 - notes are optional; the daily still gets its plan without them
            log.exception("find_notes failed")
            return ""
        if text or asyncio.get_running_loop().time() >= deadline:
            return text
        await asyncio.sleep(poll_s)
