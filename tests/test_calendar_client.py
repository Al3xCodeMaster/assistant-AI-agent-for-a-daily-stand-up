import asyncio
import json
import unittest

import httpx

import calendar_client

URL = "https://cal.example.com/mcp"
MEET = "https://meet.google.com/hsv-jcfa-ahk"
NAMES = ["Ana Pérez", "ana@example.com", "Luis Gómez", "Carla Ruiz", "Diego Carabali"]


def fake_server(attendees, *, sse=False, is_error=False):
    """Minimal MCP Streamable HTTP server: initialize, initialized, tools/call get_event_attendees."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((body.get("method"), request.headers.get("authorization"),
                     request.headers.get("mcp-session-id")))
        method = body.get("method")
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {"name": "t", "version": "1"}}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result},
                                  headers={"mcp-session-id": "sess-1"})
        if method == "tools/call":
            assert body["params"]["name"] == "get_event_attendees"
            assert body["params"]["arguments"] == {"meeting_url": MEET}
            if is_error:
                result = {"isError": True, "content": [{"type": "text", "text": "boom"}]}
            else:
                result = {"content": [{"type": "text", "text": json.dumps({"attendees": attendees})}],
                          "structuredContent": {"attendees": attendees}, "isError": False}
            msg = {"jsonrpc": "2.0", "id": body["id"], "result": result}
            if sse:
                return httpx.Response(200, text=f"event: message\ndata: {json.dumps(msg)}\n\n",
                                      headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json=msg)
        return httpx.Response(400)

    return handler, seen


def run(handler, token="tok"):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await calendar_client.list_attendees(URL, token, MEET, http=http)
    return asyncio.run(go())


class CalendarClientTests(unittest.TestCase):
    def test_returns_full_list_from_json_reply(self):
        handler, seen = fake_server(NAMES)
        self.assertEqual(run(handler), NAMES)
        methods = [m for m, _, _ in seen]
        self.assertEqual(methods, ["initialize", "notifications/initialized", "tools/call"])
        self.assertEqual(seen[0][1], "Bearer tok")
        self.assertEqual(seen[-1][2], "sess-1")  # session id carried after initialize

    def test_parses_sse_reply(self):
        handler, _ = fake_server(NAMES, sse=True)
        self.assertEqual(run(handler), NAMES)

    def test_empty_list_when_no_event(self):
        handler, _ = fake_server([])
        self.assertEqual(run(handler), [])

    def test_tool_error_raises(self):
        handler, _ = fake_server([], is_error=True)
        with self.assertRaises(RuntimeError):
            run(handler)

    def test_no_token_sends_no_authorization(self):
        handler, seen = fake_server(NAMES)
        run(handler, token="")
        self.assertIsNone(seen[0][1])


if __name__ == "__main__":
    unittest.main()
