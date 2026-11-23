"""Google Calendar MCP server (remote, Streamable HTTP, bearer token).

Anthropic's MCP connector calls it from the cloud, so it must sit behind public HTTPS (reverse proxy
or tunnel). Tools mirror stubs/calendar_mcp.py, so the app needs no code changes:
  get_event_attendees(meeting_url), create_event(summary, start, end, attendees)

Run:  python -m gcal_mcp.server      # http://0.0.0.0:9100/mcp
"""
import hmac
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from gcal_mcp.google_calendar import GoogleCalendar


class BearerAuth:
    """Pure ASGI wrapper: every HTTP request needs `Authorization: Bearer <token>`."""

    def __init__(self, app, token: str):
        if not token:
            raise ValueError("GCAL_MCP_TOKEN must be set")
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            header = dict(scope["headers"]).get(b"authorization", b"").decode()
            if not hmac.compare_digest(header, f"Bearer {self.token}"):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_app(gcal: GoogleCalendar, token: str):
    # DNS-rebinding protection only allows localhost, which would reject the public host.
    # The bearer token is the protection here.
    mcp = FastMCP(
        "google-calendar",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @mcp.tool()
    async def get_event_attendees(meeting_url: str) -> dict:
        """Return the invitee display names of the calendar event whose Google Meet link is meeting_url."""
        return {"attendees": await gcal.get_event_attendees(meeting_url)}

    @mcp.tool()
    async def create_event(summary: str, start: str, end: str, attendees: list[str]) -> dict:
        """Create a Google Calendar event. start/end are RFC 3339 with offset; attendees are email addresses."""
        return await gcal.create_event(summary, start, end, attendees)

    return BearerAuth(mcp.streamable_http_app(), token)


def main() -> None:
    import uvicorn

    gcal = GoogleCalendar(
        client_id=os.environ.get("GOOGLE_OAUTH_CLIENT_ID", ""),
        client_secret=os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
        refresh_token=os.environ.get("GOOGLE_REFRESH_TOKEN", ""),
        calendar_id=os.environ.get("GCAL_CALENDAR_ID", "primary"),
    )
    app = build_app(gcal, os.environ.get("GCAL_MCP_TOKEN", ""))
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("GCAL_MCP_PORT", "9100")))


if __name__ == "__main__":
    main()
