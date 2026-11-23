"""Gemini calls: detect actions, execute them through MCP, and check focus (model access in llm.py)."""
import datetime
import json
import logging
from zoneinfo import ZoneInfo

import calendar_client
import config
import llm
import mcp_direct
from session import MeetingSession, Proposal, Utterance, fmt

log = logging.getLogger("agent")


def _servers() -> list[dict]:
    return config.load_mcp_servers()


def now_line() -> str:
    """Today's date and time in the team's zone, for the model to resolve 'mañana', 'el viernes', calendar times."""
    now = datetime.datetime.now(ZoneInfo(config.TIMEZONE))
    return (f"Current date and time: {now.strftime('%Y-%m-%d %H:%M')} ({now.strftime('%A')}), "
            f"time zone {config.TIMEZONE}. Resolve relative dates against this. "
            f"Calendar times must be RFC 3339 with the UTC offset of this zone.")


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
                        "kind": {"type": "string", "enum": ["status_change", "other"],
                                 "description": "status_change only when the action moves one existing work package to another status."},
                    },
                    "required": ["summary", "instructions", "server", "evidence", "kind"],
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
When someone says an existing HU changes state ("pasa a en curso", "ya se cerró", "quedó probada"),
propose it with kind=status_change, naming the HU id and the target status name exactly as in OpenProject.
In a daily, the "Blocked HU" list names open OpenProject work packages that block the team. When the
transcript discusses one of them, propose a concrete unblocking step for the openproject server:
add_comment with the decision, update_work_package to change status/assignee (status_id comes from the
openproject list_statuses tool: call it first, never guess ids), or create_work_package
for a follow-up task. When the blocker needs a clarification session, propose a calendar create_event
meeting titled with the HU id and invite the people involved.
Transcripts come from speech-to-text and may contain errors; ignore fragments you can't interpret.
Write summary, instructions and evidence in Spanish, the language of the meeting."""


async def detect_actions(s: MeetingSession, new: list[Utterance], context: list[Utterance]) -> list[Proposal]:
    servers = _servers()
    if not servers or not new:
        return []
    catalog = "\n".join(f"- {x['name']}: {x.get('description', '')}" for x in servers)
    existing = "\n".join(f"- #{p.id} {p.summary} ({p.status})" for p in s.proposals.values()) or "(none)"
    prompt = (
        f"{now_line()}\n\nMeeting agenda: {s.agenda}\n\nAvailable MCP servers:\n{catalog}\n\n"
        f"Blocked HU:\n{blocked_lines(s.blocked)}\n\n"
        f"Team (for invites and assignees; use the email for calendar attendees):\n{team_lines()}\n\n"
        f"Already proposed:\n{existing}\n\nEarlier context:\n{fmt(context) or '(none)'}\n\n"
        f"NEW transcript to analyse:\n{fmt(new)}"
    )
    args = await llm.forced_call(config.ACTION_MODEL, DETECT_SYSTEM, prompt, PROPOSE_TOOL, 1024)
    valid = {x["name"] for x in servers}
    out = []
    for a in args.get("actions", []):
        if a.get("server") in valid:
            kind = a.get("kind") if a.get("kind") in ("status_change", "other") else "other"
            out.append(s.new_proposal(**{k: a[k] for k in ("summary", "instructions", "server", "evidence")}, kind=kind))
    return out


# ---------------------------------------------------------------------------
# 2. Execution through the MCP connector (side effects happen here)
# ---------------------------------------------------------------------------
EXEC_SYSTEM = """You carry out one approved action from a live meeting using the MCP tools provided.
Do exactly what the instructions say, nothing more. If information is missing (e.g. no owner or
date), make the most reasonable choice and say what you assumed. Finish with ONE short sentence
describing what you did in Spanish, including any link or ID returned by the tool."""


async def execute_action(s: MeetingSession, p: Proposal) -> tuple[str, list[dict]]:
    """Returns (one-line result, MCP tool calls made as {server, tool, is_error})."""
    servers = [x for x in _servers() if x["name"] == p.server] or _servers()
    prompt = (
        f"{now_line()}\nMeeting agenda: {s.agenda}\nAction #{p.id}: {p.summary}\n"
        f"Instructions: {p.instructions}\nTranscript evidence: \"{p.evidence}\""
    )
    return await llm.run_tools(config.ACTION_MODEL, EXEC_SYSTEM, prompt, servers, 2048)


async def fetch_attendees(meeting_url: str) -> list[str] | None:
    """Invitee names from the calendar event that holds this Meet link, read straight from the calendar
    MCP server (no Claude in the loop, so no names dropped). None on any failure (caller falls back)."""
    servers = [x for x in _servers() if x["name"] == "calendar"]
    if not servers:
        return None
    try:
        names = await calendar_client.list_attendees(
            servers[0]["url"], servers[0].get("authorization_token", ""), meeting_url)
    except Exception:  # noqa: BLE001 - calendar is optional; fall back to the speakers seen
        log.exception("fetch_attendees failed")
        return None
    log.info("calendar attendees: %d invited", len(names))
    return names or None


# ---------------------------------------------------------------------------
# 3. Project in discussion (labels the meeting so notes can be grouped per project)
# ---------------------------------------------------------------------------
PROJECT_TOOL = {
    "name": "report_project",
    "description": "Name the project the meeting is discussing right now.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project": {"type": "string", "description": "Short project name as people say it; empty if unclear."},
        },
        "required": ["project"],
    },
}

PROJECT_SYSTEM = """You label a live meeting with the project it is discussing right now.
Prefer one of the known projects when it matches; otherwise use the short name people use.
Return an empty string when the talk is not about a project (small talk, logistics).
Transcripts come from speech-to-text and may contain errors. Answer in Spanish."""


async def detect_project(text: str, known: list[str]) -> str:
    prompt = f"Known projects: {', '.join(known) or '(none yet)'}\n\nTranscript:\n{text}"
    args = await llm.forced_call(config.FOCUS_MODEL, PROJECT_SYSTEM, prompt, PROJECT_TOOL, 120)
    return (args.get("project") or "").strip()


# ---------------------------------------------------------------------------
# 4. Focus monitor
# ---------------------------------------------------------------------------
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
'drifting' = a tangent that has gone on for a while; 'off_topic' = clearly unrelated to every agenda item.
Write reason and suggestion in Spanish."""


async def check_focus(s: MeetingSession, window: list[Utterance]) -> dict:
    prompt = f"Agenda: {s.agenda}\n\nLast few minutes:\n{fmt(window)}"
    return await llm.forced_call(config.FOCUS_MODEL, FOCUS_SYSTEM, prompt, FOCUS_TOOL, 300)


# ---------------------------------------------------------------------------
# 5. OpenProject (daily): blocked HU in, results out. Direct MCP calls, no Claude in the loop.
# ---------------------------------------------------------------------------
def _openproject_server() -> dict | None:
    servers = [x for x in _servers() if x["name"] == "openproject"]
    return servers[0] if servers else None


def team_lines() -> str:
    """Team members the detector may invite or assign. Empty when the users file is missing."""
    try:
        with open(config.OPENPROJECT_USERS_FILE, encoding="utf-8") as fh:
            people = json.load(fh)
    except (OSError, ValueError):
        return "(none)"
    return "\n".join(f"- {p['name']} <{p['email']}> (OpenProject id {p['openproject_id']})" for p in people) or "(none)"


def blocked_lines(blocked: list[dict]) -> str:
    if not blocked:
        return "(none)"
    return "\n".join(
        f"- HU #{b['id']} {b['subject']} (status {b.get('status') or '?'}, "
        f"assignee {b.get('assignee') or 'none'}, blocked by {', '.join(b.get('blocked_by', []))})"
        for b in blocked
    )


async def fetch_blocked(project_id: str) -> list[dict] | None:
    """Open HU blocked in the OpenProject project. None when OpenProject is not configured or fails."""
    server = _openproject_server()
    if not server or not project_id:
        return None
    try:
        data = await mcp_direct.call_tool(server["url"], server.get("authorization_token", ""),
                                          "list_blocked_work_packages", {"project_id": project_id})
    except Exception:  # noqa: BLE001 - the daily runs without the HU list
        log.exception("fetch_blocked failed")
        return None
    return data.get("blocked", [])


async def openproject_call(tool: str, arguments: dict) -> dict:
    """One OpenProject MCP tool call (e.g. add_comment). Raises when OpenProject is not configured."""
    server = _openproject_server()
    if not server:
        raise RuntimeError("openproject MCP server not configured")
    return await mcp_direct.call_tool(server["url"], server.get("authorization_token", ""), tool, arguments)


UNBLOCK_TOOL = {
    "name": "report_unblock_plan",
    "description": "Step-by-step plan to unblock each HU that the meeting discussed.",
    "input_schema": {
        "type": "object",
        "properties": {
            "plans": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "hu_id": {"type": "string", "description": "Id of the blocked HU, as listed."},
                        "owner": {"type": "string", "description": "Who acts first; 'por definir' if not said."},
                        "steps": {"type": "array", "items": {"type": "string"},
                                  "description": "Ordered, concrete actions. Include dates when mentioned."},
                    },
                    "required": ["hu_id", "owner", "steps"],
                },
            }
        },
        "required": ["plans"],
    },
}

UNBLOCK_SYSTEM = """You turn a daily stand-up into a step-by-step plan to unblock each blocked HU.
Only plan for HU in the blocked list that the meeting actually discussed. Each step is one concrete
action (who does what, by when). Use the meeting notes first, the transcript for what they miss.
Transcripts come from speech-to-text and may contain errors. Write steps in Spanish."""


async def plan_unblock(blocked: list[dict], transcript: str, notes: str) -> list[dict]:
    prompt = (
        f"Blocked HU:\n{blocked_lines(blocked)}\n\n"
        f"Meeting notes (Gemini):\n{notes or '(none)'}\n\n"
        f"Transcript:\n{transcript or '(none)'}"
    )
    args = await llm.forced_call(config.ACTION_MODEL, UNBLOCK_SYSTEM, prompt, UNBLOCK_TOOL, 2048)
    return args.get("plans", [])
