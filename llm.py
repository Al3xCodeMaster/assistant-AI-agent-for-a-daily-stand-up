"""Gemini access for the app (google-genai SDK): one forced-function call, and a tool loop over MCP servers.

Reads GEMINI_API_KEY from the environment (the SDK also accepts GOOGLE_API_KEY).
The app's prompts and tool schemas stay in agent.py; this module only moves them to Gemini and back.
"""
import logging

from google import genai
from google.genai import types

import mcp_direct

log = logging.getLogger("llm")
_client: genai.Client | None = None
MAX_TOOL_TURNS = 8  # guard against a model that keeps calling tools forever


def client() -> genai.Client:
    global _client
    if _client is None:
        # Function calling is served on v1beta; v1 rejects it with INVALID_ARGUMENT.
        _client = genai.Client(http_options=types.HttpOptions(api_version="v1beta"))
    return _client


def _declaration(name: str, description: str, schema: dict) -> types.FunctionDeclaration:
    return types.FunctionDeclaration(name=name, description=description, parameters_json_schema=schema)


def _config(system: str, declarations: list[types.FunctionDeclaration], max_tokens: int,
            mode: str = "AUTO", only: list[str] | None = None) -> types.GenerateContentConfig:
    tools = [types.Tool(function_declarations=declarations)] if declarations else None
    fc = types.FunctionCallingConfig(mode=mode, allowed_function_names=only) if tools else None
    return types.GenerateContentConfig(
        system_instruction=system,
        max_output_tokens=max_tokens,
        tools=tools,
        tool_config=types.ToolConfig(function_calling_config=fc) if tools else None,
    )


async def forced_call(model: str, system: str, prompt: str, tool: dict, max_tokens: int) -> dict:
    """Make the model call one function and return its arguments. {} when it did not call it.

    `tool` uses the same shape the app already had: name, description, input_schema (JSON Schema).
    Mode ANY with the one allowed function: a text-only answer is not possible, so the arguments always come back.
    """
    resp = await client().aio.models.generate_content(
        model=model,
        contents=prompt,
        config=_config(system, [_declaration(tool["name"], tool["description"], tool["input_schema"])], max_tokens,
                       mode="ANY", only=[tool["name"]]),
    )
    for call in resp.function_calls or []:
        if call.name == tool["name"]:
            return dict(call.args or {})
    return {}


def _mcp_name(server: str, tool: str) -> str:
    """Gemini function names: letters, digits, underscore. Server and tool names are joined with '__'."""
    return f"{server}__{tool}".replace("-", "_").replace(".", "_")[:64]


async def run_tools(model: str, system: str, prompt: str, servers: list[dict], max_tokens: int) -> tuple[str, list[dict]]:
    """Run an agent loop: the model calls MCP tools of the given servers until it answers in text.

    Returns (final text, calls), where each call is {server, tool, is_error}, like the old connector reported.
    Tool failures are sent back to the model as errors; only transport failures of the server raise.
    """
    routes: dict[str, tuple[dict, str]] = {}  # gemini name -> (server, real tool name)
    declarations = []
    for server in servers:
        tools = await mcp_direct.list_tools(server["url"], server.get("authorization_token", ""))
        for t in tools:
            name = _mcp_name(server["name"], t["name"])
            routes[name] = (server, t["name"])
            declarations.append(_declaration(name, t.get("description", "") or t["name"],
                                             t.get("inputSchema") or {"type": "object", "properties": {}}))

    contents: list[types.Content] = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]
    calls: list[dict] = []
    cfg = _config(system, declarations, max_tokens)
    for _ in range(MAX_TOOL_TURNS):
        resp = await client().aio.models.generate_content(model=model, contents=contents, config=cfg)
        if not resp.function_calls:
            return (resp.text or "Done.").strip(), calls
        if resp.candidates and resp.candidates[0].content:
            contents.append(resp.candidates[0].content)
        replies = []
        for fc in resp.function_calls:
            args = dict(fc.args or {})
            if fc.name not in routes:
                replies.append(types.Part.from_function_response(name=fc.name, response={"error": "unknown tool"}))
                calls.append({"server": "", "tool": fc.name, "is_error": True})
                continue
            server, tool = routes[fc.name]
            try:
                result = await mcp_direct.call_result(server["url"], server.get("authorization_token", ""), tool, args)
                is_error = bool(result.get("isError"))
                payload = {"error" if is_error else "result": result.get("content", [])}
            except Exception as exc:  # noqa: BLE001 - a failed call goes back to the model as an error
                log.exception("MCP call %s.%s failed", server["name"], tool)
                is_error, payload = True, {"error": str(exc)}
            calls.append({"server": server["name"], "tool": tool, "is_error": is_error})
            replies.append(types.Part.from_function_response(name=fc.name, response=payload))
        contents.append(types.Content(role="user", parts=replies))
    log.warning("tool loop stopped after %d turns", MAX_TOOL_TURNS)
    return "Done.", calls
