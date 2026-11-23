"""Tests for the local Calendar MCP stub (stubs/calendar_mcp.py).

Needs the stub requirements (mcp, uvicorn). Skipped when they are not installed.
Run:  python -m unittest tests.test_calendar_stub
"""
import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

try:
    import httpx
    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.testclient import TestClient
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise unittest.SkipTest(f"calendar stub dependencies missing: {exc}")

from stubs.calendar_mcp import BearerAuth, build_app, find_attendees, load_fixture  # noqa: E402

TOKEN = "stub-test-token"
FIXTURE = Path(ROOT) / "stubs" / "calendar_fixture.json"
DAILY_LINK = "https://meet.google.com/aaa-bbbb-ccc"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FindAttendeesTests(unittest.TestCase):
    def setUp(self):
        self.events = load_fixture(FIXTURE)

    def test_returns_display_names_for_matching_link(self):
        self.assertEqual(find_attendees(self.events, DAILY_LINK), ["Ana", "Luis", "Carla"])

    def test_link_match_ignores_case_and_trailing_slash(self):
        self.assertEqual(find_attendees(self.events, DAILY_LINK.upper() + "/"), ["Ana", "Luis", "Carla"])

    def test_falls_back_to_email_when_no_display_name(self):
        self.assertIn("po@example.com", find_attendees(self.events, "https://meet.google.com/ddd-eeee-fff"))

    def test_unknown_link_returns_empty_list(self):
        self.assertEqual(find_attendees(self.events, "https://meet.google.com/zzz-zzzz-zzz"), [])


class BearerAuthTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(BearerAuth(_route_app(), TOKEN))

    def test_rejects_missing_token(self):
        self.assertEqual(self.client.post("/mcp").status_code, 401)

    def test_rejects_wrong_token(self):
        r = self.client.post("/mcp", headers={"Authorization": "Bearer nope"})
        self.assertEqual(r.status_code, 401)

    def test_accepts_correct_token(self):
        r = self.client.post("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(r.status_code, 200)

    def test_refuses_empty_token(self):
        with self.assertRaises(ValueError):
            BearerAuth(_route_app(), "")


def _route_app():
    from starlette.routing import Route

    async def ok(request):
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/mcp", ok, methods=["GET", "POST"])])


class StubServerRoundTripTests(unittest.TestCase):
    """Starts the real stub under uvicorn and talks to it with the official MCP client."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "calls.jsonl"
        self.port = free_port()
        app = build_app(load_fixture(FIXTURE), self.log, TOKEN)
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.server.started, "stub server did not start")
        self.url = f"http://127.0.0.1:{self.port}/mcp"

    def tearDown(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.tmp.cleanup()

    async def _session(self, token, body):
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(headers=headers) as http:
            async with streamable_http_client(self.url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await body(session)

    def run_session(self, body, token=TOKEN):
        return asyncio.run(self._session(token, body))

    def test_lists_both_tools(self):
        async def body(session):
            return {t.name for t in (await session.list_tools()).tools}

        self.assertEqual(self.run_session(body), {"get_event_attendees", "create_event"})

    def test_get_event_attendees_over_mcp(self):
        async def body(session):
            result = await session.call_tool("get_event_attendees", {"meeting_url": DAILY_LINK})
            return json.loads(result.content[0].text)

        data = self.run_session(body)
        self.assertEqual(data["attendees"], ["Ana", "Luis", "Carla"])

    def test_create_event_is_recorded_not_created(self):
        async def body(session):
            args = {"summary": "Ayuda HU-123", "start": "2026-11-03T10:00:00-05:00",
                    "end": "2026-11-03T10:20:00-05:00", "attendees": ["Luis", "Ana"]}
            result = await session.call_tool("create_event", args)
            return json.loads(result.content[0].text)

        data = self.run_session(body)
        self.assertEqual(data["status"], "confirmed")
        self.assertTrue(data["id"].startswith("stub-"))
        entries = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e["tool"] for e in entries], ["create_event"])
        self.assertEqual(entries[0]["arguments"]["summary"], "Ayuda HU-123")

    def test_wrong_token_is_refused(self):
        async def body(session):
            return await session.list_tools()

        with self.assertRaises(Exception):
            self.run_session(body, token="wrong")


if __name__ == "__main__":
    unittest.main()
