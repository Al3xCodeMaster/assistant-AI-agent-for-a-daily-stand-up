"""Direct MCP tool call (JSON-RPC over Streamable HTTP), no Claude in the loop.

Used when the app itself needs data or must write to a server (OpenProject reads, metric posts).
Same protocol flow as calendar_client.list_attendees, generalised to any tool.
"""
import json

import httpx

PROTOCOL_VERSION = "2025-03-26"


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
    if "text/event-stream" in resp.headers.get("content-type", ""):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                msg = json.loads(line[5:].strip())
                if msg.get("id") == req_id:
                    return _check(msg, req_id)
        raise RuntimeError(f"no JSON-RPC reply with id {req_id} in SSE stream")
    return _check(resp.json(), req_id)


def _check(msg: dict, req_id: int) -> dict:
    if "error" in msg:
        raise RuntimeError(f"MCP error for id {req_id}: {msg['error']}")
    return msg.get("result", {})


async def _request(url: str, token: str, method: str, params: dict | None,
                   http: httpx.AsyncClient | None) -> dict:
    """One JSON-RPC request inside a fresh MCP session. Returns the result object."""
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
        call = await http.post(url, headers=_headers(token, session),
                               json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params or {}})
        call.raise_for_status()
        return _rpc_result(call, 2)
    finally:
        if own:
            await http.aclose()


async def list_tools(url: str, token: str, http: httpx.AsyncClient | None = None) -> list[dict]:
    """Tools a server offers: each has name, description and inputSchema (JSON Schema)."""
    result = await _request(url, token, "tools/list", {}, http)
    return result.get("tools", [])


async def call_result(url: str, token: str, tool: str, arguments: dict,
                      http: httpx.AsyncClient | None = None) -> dict:
    """Raw MCP tool result: content list and isError flag. Raises only on transport or JSON-RPC failure."""
    return await _request(url, token, "tools/call", {"name": tool, "arguments": arguments}, http)


async def call_tool(url: str, token: str, tool: str, arguments: dict,
                    http: httpx.AsyncClient | None = None) -> dict:
    """Structured result of one MCP tool. Raises on transport, JSON-RPC or tool (isError) failure."""
    result = await call_result(url, token, tool, arguments, http)
    if result.get("isError"):
        raise RuntimeError(f"{tool} returned isError: {result.get('content')}")
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])
