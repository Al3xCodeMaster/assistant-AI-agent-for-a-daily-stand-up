"""Direct MCP call to the calendar server (gcal_mcp) for the invitee list.

No Claude in the loop: the app sends `tools/call get_event_attendees` itself, so the full list
comes back exactly as the server returns it. Speaks MCP Streamable HTTP (JSON-RPC over POST),
replies as JSON or as a single SSE message.
"""
import json

import httpx

PROTOCOL_VERSION = "2025-03-26"
TOOL = "get_event_attendees"


def _headers(token: str, session_id: str = "") -> dict:
    h = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    if session_id:
        h["Mcp-Session-Id"] = session_id
    return h


def _rpc_result(resp: httpx.Response, req_id: int) -> dict:
    """Result of the JSON-RPC reply with this id, from a JSON body or an SSE stream."""
    if "text/event-stream" in resp.headers.get("content-type", ""):
        messages = [json.loads(line[5:]) for line in resp.text.splitlines()
                    if line.startswith("data:") and line[5:].strip()]
    else:
        messages = [resp.json()]
    for msg in messages:
        if msg.get("id") == req_id:
            if "error" in msg:
                raise RuntimeError(f"MCP error: {msg['error']}")
            return msg["result"]
    raise RuntimeError(f"MCP reply {req_id} missing")


async def list_attendees(url: str, token: str, meeting_url: str, http: httpx.AsyncClient | None = None) -> list[str]:
    """Invitee names for the event with this Meet link. [] when no event matches. Raises on transport/MCP errors."""
    own = http is None
    http = http or httpx.AsyncClient(timeout=30)
    try:
        init = await http.post(url, headers=_headers(token), json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": {"name": "meet-copilot", "version": "1.0"}},
        })
        init.raise_for_status()
        _rpc_result(init, 1)
        session = init.headers.get("mcp-session-id", "")
        await http.post(url, headers=_headers(token, session),
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        call = await http.post(url, headers=_headers(token, session), json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": TOOL, "arguments": {"meeting_url": meeting_url}},
        })
        call.raise_for_status()
        result = _rpc_result(call, 2)
    finally:
        if own:
            await http.aclose()
    if result.get("isError"):
        raise RuntimeError(f"{TOOL} returned isError: {result.get('content')}")
    data = result.get("structuredContent") or json.loads(result["content"][0]["text"])
    names = data.get("attendees")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise RuntimeError(f"unexpected {TOOL} payload: {data!r}")
    return names
