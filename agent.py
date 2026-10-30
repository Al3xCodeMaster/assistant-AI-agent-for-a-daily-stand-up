"""Claude calls: detect actions, execute them through MCP, and check focus."""
import logging

import config
from session import MeetingSession, Proposal, Utterance, fmt

log = logging.getLogger("agent")
_client = None


def client():
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic

        _client = AsyncAnthropic()
    return _client


def _servers() -> list[dict]:
    return config.load_mcp_servers()


def _api_server(s: dict) -> dict:
    """Strip our local-only fields (like 'description') before sending to the API."""
    out = {"type": "url", "url": s["url"], "name": s["name"]}
    if s.get("authorization_token"):
        out["authorization_token"] = s["authorization_token"]
    return out


def _forced_tool_input(resp, tool_name: str) -> dict:
    for block in resp.content:
        if block.type == "tool_use" and block.name == tool_name:
            return block.input
    return {}


# ---------------------------------------------------------------------------
# 1. Action detection (no side effects: only proposes)
# ---------------------------------------------------------------------------
PROPOSE_TOOL = {
    "name": "propose_actions",
    "description": "Report concrete actions the meeting just asked for. Empty list if none.",
    "input_schema": {
        "type": "object",
        "properties": {
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string", "description": "One line, under 120 chars, shown in Meet chat."},
                        "instructions": {"type": "string", "description": "Exact, self-contained instructions for an agent with the MCP tools: titles, owners, dates, recipients."},
                        "server": {"type": "string", "description": "Name of the MCP server that should handle it."},
                        "evidence": {"type": "string", "description": "Short quote from the transcript that triggered it."},
                    },
                    "required": ["summary", "instructions", "server", "evidence"],
                },
            }
        },
        "required": ["actions"],
    },
}

DETECT_SYSTEM = """You monitor a live meeting transcript for things the participants want DONE.
Propose an action only when it is concrete and clearly intended: an explicit task with an owner,
a decision someone asked to record, a request to schedule/send/file/look up something.
Do NOT propose actions for brainstorming, hypotheticals, jokes, or things already proposed.
Only use the MCP servers listed. If nothing qualifies, return an empty list.
Transcripts come from speech-to-text and may contain errors; ignore fragments you can't interpret."""


async def detect_actions(s: MeetingSession, new: list[Utterance], context: list[Utterance]) -> list[Proposal]:
    servers = _servers()
    if not servers or not new:
        return []
    catalog = "\n".join(f"- {x['name']}: {x.get('description', '')}" for x in servers)
    existing = "\n".join(f"- #{p.id} {p.summary} ({p.status})" for p in s.proposals.values()) or "(none)"
    prompt = (
        f"Meeting agenda: {s.agenda}\n\nAvailable MCP servers:\n{catalog}\n\n"
        f"Already proposed:\n{existing}\n\nEarlier context:\n{fmt(context) or '(none)'}\n\n"
        f"NEW transcript to analyse:\n{fmt(new)}"
    )
    resp = await client().messages.create(
        model=config.ACTION_MODEL,
        max_tokens=1024,
        system=DETECT_SYSTEM,
        tools=[PROPOSE_TOOL],
        tool_choice={"type": "tool", "name": "propose_actions"},
        messages=[{"role": "user", "content": prompt}],
    )
    valid = {x["name"] for x in servers}
    out = []
    for a in _forced_tool_input(resp, "propose_actions").get("actions", []):
        if a.get("server") in valid:
            out.append(s.new_proposal(**{k: a[k] for k in ("summary", "instructions", "server", "evidence")}))
    return out


# ---------------------------------------------------------------------------
# 2. Execution through the MCP connector (side effects happen here)
# ---------------------------------------------------------------------------
EXEC_SYSTEM = """You carry out one approved action from a live meeting using the MCP tools provided.
Do exactly what the instructions say, nothing more. If information is missing (e.g. no owner or
date), make the most reasonable choice and say what you assumed. Finish with ONE short sentence
describing what you did, including any link or ID returned by the tool."""


def mcp_tool_calls(content) -> list[dict]:
    """Pair mcp_tool_use blocks with their mcp_tool_result to see which calls failed."""
    errors = {b.tool_use_id: bool(getattr(b, "is_error", False))
              for b in content if b.type == "mcp_tool_result"}
    return [{"server": b.server_name, "tool": b.name, "is_error": errors.get(b.id, False)}
            for b in content if b.type == "mcp_tool_use"]


async def execute_action(s: MeetingSession, p: Proposal) -> tuple[str, list[dict]]:
    """Returns (one-line result, MCP tool calls made)."""
    servers = [x for x in _servers() if x["name"] == p.server] or _servers()
    resp = await client().beta.messages.create(
        model=config.ACTION_MODEL,
        max_tokens=2048,
        system=EXEC_SYSTEM,
        betas=[config.MCP_BETA],
        mcp_servers=[_api_server(x) for x in servers],
        tools=[{"type": "mcp_toolset", "mcp_server_name": x["name"]} for x in servers],
        messages=[{
            "role": "user",
            "content": (
                f"Meeting agenda: {s.agenda}\nAction #{p.id}: {p.summary}\n"
                f"Instructions: {p.instructions}\nTranscript evidence: \"{p.evidence}\""
            ),
        }],
    )
    texts = [b.text for b in resp.content if b.type == "text"]
    return (texts[-1] if texts else "Done.").strip(), mcp_tool_calls(resp.content)


# ---------------------------------------------------------------------------
# 3. Focus monitor
# ---------------------------------------------------------------------------
FOCUS_TOOL = {
    "name": "report_focus",
    "description": "Classify whether the last minutes of the meeting follow the agenda.",
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["on_topic", "drifting", "off_topic"]},
            "current_topic": {"type": "string", "description": "What is being discussed, in a few words."},
            "reason": {"type": "string", "description": "One short sentence."},
            "suggestion": {"type": "string", "description": "A friendly one-line nudge back to the agenda, naming the next agenda item."},
        },
        "required": ["status", "current_topic", "reason", "suggestion"],
    },
}

FOCUS_SYSTEM = """You help a facilitator keep a meeting on its agenda. Small talk at the very start,
brief tangents that clearly support an agenda item, and logistics are fine (on_topic).
'drifting' = a tangent that has gone on for a while; 'off_topic' = clearly unrelated to every agenda item."""


async def check_focus(s: MeetingSession, window: list[Utterance]) -> dict:
    resp = await client().messages.create(
        model=config.FOCUS_MODEL,
        max_tokens=300,
        system=FOCUS_SYSTEM,
        tools=[FOCUS_TOOL],
        tool_choice={"type": "tool", "name": "report_focus"},
        messages=[{"role": "user", "content": f"Agenda: {s.agenda}\n\nLast few minutes:\n{fmt(window)}"}],
    )
    return _forced_tool_input(resp, "report_focus")
