"""Google Calendar API v3 calls for the Calendar MCP server.

Plain httpx, no MCP imports, so it can be tested without the MCP SDK. The refresh token belongs to
one Google account; every call uses that account's `calendar_id` (default: its primary calendar).
"""
import datetime as dt
import time
import uuid

import httpx

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/calendar/v3"
SEARCH_HOURS = 24  # look this far before and after now for the event that holds the Meet link


def normalize_url(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def attendee_names(event: dict) -> list[str]:
    """Display names (email when no name) of people invited, skipping rooms and other resources."""
    return [a.get("displayName") or a.get("email", "")
            for a in event.get("attendees", []) if not a.get("resource")]


class GoogleCalendar:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str,
                 calendar_id: str = "primary", http: httpx.AsyncClient | None = None, clock=time.time):
        if not (client_id and client_secret and refresh_token):
            raise ValueError("GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN must be set")
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.calendar_id = calendar_id
        self.http = http or httpx.AsyncClient(timeout=20)
        self.clock = clock
        self._access_token = ""
        self._expires_at = 0.0

    async def _access(self) -> str:
        """Access token from the refresh token; cached until a minute before it expires."""
        if self._access_token and self.clock() < self._expires_at - 60:
            return self._access_token
        r = await self.http.post(TOKEN_URL, data={
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        })
        r.raise_for_status()
        body = r.json()
        self._access_token = body["access_token"]
        self._expires_at = self.clock() + int(body.get("expires_in", 3600))
        return self._access_token

    async def _call(self, method: str, path: str, **kwargs) -> dict:
        token = await self._access()
        r = await self.http.request(method, f"{API_BASE}/calendars/{self.calendar_id}{path}",
                                    headers={"Authorization": f"Bearer {token}"}, **kwargs)
        r.raise_for_status()
        return r.json()

    async def find_event_by_meet(self, meeting_url: str) -> dict | None:
        """The event whose Google Meet link equals meeting_url, searching a day either side of now."""
        target = normalize_url(meeting_url)
        if not target:
            return None
        now = dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc)
        params = {
            "timeMin": (now - dt.timedelta(hours=SEARCH_HOURS)).isoformat(),
            "timeMax": (now + dt.timedelta(hours=SEARCH_HOURS)).isoformat(),
            "singleEvents": "true",
            "maxResults": "250",
        }
        while True:
            data = await self._call("GET", "/events", params=params)
            for event in data.get("items", []):
                if normalize_url(event.get("hangoutLink", "")) == target:
                    return event
            page = data.get("nextPageToken")
            if not page:
                return None
            params["pageToken"] = page

    async def get_event_attendees(self, meeting_url: str) -> list[str]:
        """Invitee names of the event with this Meet link; [] when no event matches."""
        event = await self.find_event_by_meet(meeting_url)
        return attendee_names(event) if event else []

    async def create_event(self, summary: str, start: str, end: str, attendees: list[str]) -> dict:
        """Create an event with RFC 3339 start/end (with offset). Invitations are sent to attendees."""
        # conferenceData asks Google to create a Meet link: the bot needs one to join the meeting
        body = {
            "summary": summary,
            "start": {"dateTime": start},
            "end": {"dateTime": end},
            "attendees": [{"email": email} for email in attendees],
            "conferenceData": {"createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }},
        }
        data = await self._call("POST", "/events", params={"sendUpdates": "all", "conferenceDataVersion": "1"},
                                json=body)
        return {"id": data.get("id", ""), "status": data.get("status", ""), "htmlLink": data.get("htmlLink", ""),
                "meet_url": data.get("hangoutLink", "")}
