"""Tests for the Google Calendar MCP server (gcal_mcp/).

Google is faked with httpx.MockTransport: no network, no credentials.
The client tests need only httpx; the server auth test needs the MCP SDK and is skipped without it.
Run:  python3 -m unittest tests.test_gcal_mcp
"""
import asyncio
import json
import os
import sys
import unittest

import httpx

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from gcal_mcp.google_calendar import GoogleCalendar, attendee_names  # noqa: E402

MEET = "https://meet.google.com/aaa-bbbb-ccc"
OTHER_MEET = "https://meet.google.com/ddd-eeee-fff"


def fake_google(pages: dict, created: list | None = None):
    """Handler for the token endpoint and events.list / events.insert. `pages` is keyed by pageToken ('' = first)."""
    seen = {"token_calls": 0, "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["requests"].append(request)
        if request.url.host == "oauth2.googleapis.com":
            seen["token_calls"] += 1
            return httpx.Response(200, json={"access_token": f"at-{seen['token_calls']}", "expires_in": 3600})
        if request.method == "GET" and request.url.path.endswith("/events"):
            return httpx.Response(200, json=pages[request.url.params.get("pageToken", "")])
        if request.method == "POST" and request.url.path.endswith("/events"):
            created.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "ev-1", "status": "confirmed", "htmlLink": "https://cal/ev-1"})
        return httpx.Response(404)

    return handler, seen


def make_client(handler, clock=lambda: 1_700_000_000.0):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GoogleCalendar("cid", "csecret", "refresh-1", http=http, clock=clock)


STANDUP = {
    "summary": "Daily",
    "hangoutLink": MEET,
    "attendees": [
        {"displayName": "Ana", "email": "ana@example.com"},
        {"email": "luis@example.com"},  # no display name: fall back to email
        {"displayName": "Sala 3", "email": "sala@resource.calendar.google.com", "resource": True},
    ],
}


class AttendeeTests(unittest.TestCase):
    def test_finds_event_on_second_page_and_skips_rooms(self):
        pages = {
            "": {"items": [{"summary": "Other", "hangoutLink": OTHER_MEET}], "nextPageToken": "p2"},
            "p2": {"items": [STANDUP]},
        }
        handler, seen = fake_google(pages)
        gcal = make_client(handler)
        names = asyncio.run(gcal.get_event_attendees(MEET))
        self.assertEqual(names, ["Ana", "luis@example.com"])
        self.assertEqual(seen["token_calls"], 1)  # access token reused for every page
        auth = [r.headers["authorization"] for r in seen["requests"] if r.url.host == "www.googleapis.com"]
        self.assertTrue(all(a == "Bearer at-1" for a in auth))
        self.assertEqual(len(auth), 2)  # both pages were requested
        self.assertEqual(seen["requests"][-1].url.params["singleEvents"], "true")

    def test_no_matching_event_returns_empty(self):
        handler, _ = fake_google({"": {"items": [{"hangoutLink": OTHER_MEET}]}})
        self.assertEqual(asyncio.run(make_client(handler).get_event_attendees(MEET)), [])

    def test_link_match_ignores_case_and_trailing_slash(self):
        handler, _ = fake_google({"": {"items": [STANDUP]}})
        names = asyncio.run(make_client(handler).get_event_attendees(MEET.upper() + "/"))
        self.assertEqual(names, ["Ana", "luis@example.com"])

    def test_attendee_names_handles_event_without_attendees(self):
        self.assertEqual(attendee_names({"summary": "solo"}), [])


class CreateEventTests(unittest.TestCase):
    def test_create_sends_invitations_and_returns_id(self):
        created: list = []
        handler, seen = fake_google({}, created)
        result = asyncio.run(make_client(handler).create_event(
            "Follow-up", "2026-11-03T10:00:00-05:00", "2026-11-03T10:30:00-05:00", ["ana@example.com"]))
        self.assertEqual(result["id"], "ev-1")
        self.assertEqual(created[0]["attendees"], [{"email": "ana@example.com"}])
        self.assertEqual(created[0]["start"], {"dateTime": "2026-11-03T10:00:00-05:00"})
        post = [r for r in seen["requests"] if r.method == "POST" and r.url.host == "www.googleapis.com"][0]
        self.assertEqual(post.url.params["sendUpdates"], "all")


class TokenTests(unittest.TestCase):
    def test_access_token_renewed_after_expiry(self):
        now = {"t": 1_000.0}
        handler, seen = fake_google({"": {"items": []}})
        gcal = make_client(handler, clock=lambda: now["t"])
        asyncio.run(gcal.get_event_attendees(MEET))
        now["t"] += 3600  # past expires_in
        asyncio.run(gcal.get_event_attendees(MEET))
        self.assertEqual(seen["token_calls"], 2)

    def test_missing_credentials_refused(self):
        with self.assertRaises(ValueError):
            GoogleCalendar("", "secret", "refresh")


class ServerAuthTests(unittest.TestCase):
    def test_requests_without_bearer_token_rejected(self):
        try:
            from starlette.testclient import TestClient

            from gcal_mcp.server import build_app
        except ImportError as exc:  # pragma: no cover - MCP SDK not installed
            self.skipTest(f"MCP SDK missing: {exc}")
        handler, _ = fake_google({})
        app = build_app(make_client(handler), "gcal-test-token")
        with TestClient(app) as client:
            self.assertEqual(client.post("/mcp", headers={"authorization": "Bearer wrong"}).status_code, 401)
            self.assertEqual(client.post("/mcp").status_code, 401)

    def test_server_refuses_empty_token(self):
        try:
            from gcal_mcp.server import BearerAuth
        except ImportError as exc:  # pragma: no cover
            self.skipTest(f"MCP SDK missing: {exc}")
        with self.assertRaises(ValueError):
            BearerAuth(None, "")


if __name__ == "__main__":
    unittest.main()
