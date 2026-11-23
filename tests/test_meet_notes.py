"""Tests for Gemini notes from Drive (meet_notes.py) and direct MCP calls (mcp_direct.py).

Google and MCP servers are faked with httpx.MockTransport: no network, no credentials.
Run:  python3 -m unittest tests.test_meet_notes
"""
import asyncio
import datetime as dt
import json
import os
import sys
import unittest

import httpx

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

import mcp_direct  # noqa: E402
import meet_notes  # noqa: E402

START = dt.datetime(2026, 11, 17, 14, 0, tzinfo=dt.timezone.utc)


def fake_drive(files: list[dict], export_text: str = "Acuerdos: desbloquear HU 12"):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        if request.url.path == "/drive/v3/files":
            return httpx.Response(200, json={"files": files})
        if request.url.path.endswith("/export"):
            return httpx.Response(200, text=export_text)
        return httpx.Response(404)

    return handler, seen


class PickDocTests(unittest.TestCase):
    def test_prefers_doc_with_meeting_title(self):
        files = [{"id": "a", "name": "Notas de Gemini: Otra reunión"},
                 {"id": "b", "name": "Notas de Gemini: Daily SIRAP"}]
        self.assertEqual(meet_notes.pick_doc(files, "daily sirap")["id"], "b")

    def test_falls_back_to_newest(self):
        files = [{"id": "a", "name": "Notas de Gemini"}, {"id": "b", "name": "Notes by Gemini"}]
        self.assertEqual(meet_notes.pick_doc(files, "")["id"], "a")

    def test_none_when_no_doc(self):
        self.assertIsNone(meet_notes.pick_doc([], "daily"))


class FindNotesTests(unittest.TestCase):
    def test_exports_matching_doc_as_text(self):
        handler, seen = fake_drive([{"id": "doc1", "name": "Notas de Gemini: Daily SIRAP"}])
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        text = asyncio.run(meet_notes.find_notes("cid", "csec", "refresh", START, "Daily SIRAP", http))
        self.assertEqual(text, "Acuerdos: desbloquear HU 12")
        query = [r for r in seen if r.url.path == "/drive/v3/files"][0].url.params["q"]
        self.assertIn("modifiedTime >", query)
        self.assertIn("Notas de Gemini", query)

    def test_empty_when_no_doc_yet(self):
        handler, _ = fake_drive([])
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.assertEqual(asyncio.run(meet_notes.find_notes("cid", "csec", "refresh", START, "", http)), "")

    def test_no_refresh_token_means_no_call(self):
        self.assertEqual(asyncio.run(meet_notes.find_notes("cid", "csec", "", START, "")), "")

    def test_wait_gives_up_after_deadline(self):
        handler, _ = fake_drive([])
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        text = asyncio.run(meet_notes.wait_for_notes("cid", "csec", "refresh", START, "", wait_s=0, poll_s=0, http=http))
        self.assertEqual(text, "")


def fake_mcp(result: dict, sse: bool = False):
    """Minimal MCP Streamable HTTP server: initialize, notifications/initialized, tools/call."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
                                  headers={"mcp-session-id": "s1"})
        if method == "notifications/initialized":
            return httpx.Response(202)
        payload = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if sse:
            return httpx.Response(200, text=f"event: message\ndata: {json.dumps(payload)}\n\n",
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=payload)

    return handler


class McpDirectTests(unittest.TestCase):
    def test_returns_structured_content(self):
        handler = fake_mcp({"structuredContent": {"blocked": [{"id": 7}]}})
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        data = asyncio.run(mcp_direct.call_tool("https://mcp.example/mcp", "tok", "list_blocked_work_packages",
                                                {"project_id": "sirap"}, http))
        self.assertEqual(data, {"blocked": [{"id": 7}]})

    def test_reads_sse_reply(self):
        handler = fake_mcp({"structuredContent": {"ok": True}}, sse=True)
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.assertEqual(asyncio.run(mcp_direct.call_tool("https://mcp.example/mcp", "", "t", {}, http)), {"ok": True})

    def test_tool_error_raises(self):
        handler = fake_mcp({"isError": True, "content": [{"type": "text", "text": "HU not found"}]})
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with self.assertRaises(RuntimeError):
            asyncio.run(mcp_direct.call_tool("https://mcp.example/mcp", "", "t", {}, http))


if __name__ == "__main__":
    unittest.main()
