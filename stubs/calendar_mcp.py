"""Local stand-in for the Google Calendar MCP server, for controlled end-to-end tests.

Serves fixed calendar data over Streamable HTTP behind a bearer token, the way Anthropic's MCP
connector calls remote servers. create_event only records the call in a JSONL log; it creates nothing.

Run:  CALENDAR_STUB_TOKEN=secret python stubs/calendar_mcp.py      # http://0.0.0.0:9000/mcp
"""
import datetime
import hmac
import json
import os
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

HERE = Path(__file__).parent
DEFAULT_FIXTURE = HERE / "calendar_fixture.json"


def load_fixture(path) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["events"]


def normalize_url(url: str) -> str:
    return url.strip().rstrip("/").lower()


def find_attendees(events: list[dict], meeting_url: str) -> list[str]:
    """Display names of the event whose Meet link matches; [] when no event matches."""
    target = normalize_url(meeting_url)
    for event in events:
        if normalize_url(event.get("hangoutLink", "")) == target:
            return [a.get("displayName") or a["email"] for a in event.get("attendees", [])]
    return []


def record_call(log_path, tool: str, arguments: dict) -> None:
    entry = {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "tool": tool, "arguments": arguments}
    with Path(log_path).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


class BearerAuth:
    """Pure ASGI wrapper: every HTTP request needs `Authorization: Bearer <token>`."""

    def __init__(self, app, token: str):
        if not token:
            raise ValueError("CALENDAR_STUB_TOKEN must be set")
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            header = dict(scope["headers"]).get(b"authorization", b"").decode()
            if not hmac.compare_digest(header, f"Bearer {self.token}"):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_app(events: list[dict], log_path, token: str):
    # DNS-rebinding protection only allows localhost, which would reject the public tunnel host.
    # The bearer token is the protection here.
    mcp = FastMCP(
        "calendar-stub",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @mcp.tool()
    def get_event_attendees(meeting_url: str) -> dict:
        """Return the invitee display names of the calendar event whose Google Meet link is meeting_url."""
        record_call(log_path, "get_event_attendees", {"meeting_url": meeting_url})
        return {"attendees": find_attendees(events, meeting_url)}

    @mcp.tool()
    def create_event(summary: str, start: str, end: str, attendees: list[str]) -> dict:
        """Create a calendar event. Stub: records the request and returns a fake id."""
        record_call(log_path, "create_event",
                    {"summary": summary, "start": start, "end": end, "attendees": attendees})
        return {"id": f"stub-{uuid.uuid4().hex[:8]}", "status": "confirmed"}

    return BearerAuth(mcp.streamable_http_app(), token)


def main() -> None:
    import uvicorn

    app = build_app(
        load_fixture(os.environ.get("CALENDAR_STUB_FIXTURE", DEFAULT_FIXTURE)),
        os.environ.get("CALENDAR_STUB_LOG", "calls.jsonl"),
        os.environ.get("CALENDAR_STUB_TOKEN", ""),
    )
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("CALENDAR_STUB_PORT", "9000")))


if __name__ == "__main__":
    main()
