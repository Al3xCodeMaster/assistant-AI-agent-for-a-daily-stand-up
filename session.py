"""Per-meeting state and parsing of Recall real-time events."""
import re
import time
from dataclasses import dataclass, field

from metrics import MeetingMetrics


@dataclass
class Utterance:
    t: float
    speaker: str
    text: str


@dataclass
class Proposal:
    id: int
    summary: str  # one line, shown in chat
    instructions: str  # what the executor should do
    server: str  # MCP server name expected to handle it
    evidence: str  # quote from the transcript
    status: str = "pending"  # pending | approved | rejected | done | failed
    result: str = ""


@dataclass
class MeetingSession:
    bot_id: str
    agenda: str
    utterances: list[Utterance] = field(default_factory=list)
    detect_cursor: int = 0  # index of first utterance not yet scanned for actions
    proposals: dict[int, Proposal] = field(default_factory=dict)
    next_proposal_id: int = 1
    drift_strikes: int = 0
    last_warning_at: float = 0.0
    last_detect_at: float = 0.0
    last_focus_at: float = 0.0
    focus_enabled: bool = True
    current_topic: str = ""
    ended: bool = False
    metrics: MeetingMetrics = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.metrics is None:
            self.metrics = MeetingMetrics(bot_id=self.bot_id)

    def add(self, speaker: str, text: str, t: float | None = None) -> None:
        if text.strip():
            self.utterances.append(Utterance(t or time.time(), speaker, text.strip()))

    def window(self, seconds: int) -> list[Utterance]:
        cutoff = time.time() - seconds
        return [u for u in self.utterances if u.t >= cutoff]

    def new_proposal(self, **kw) -> Proposal:
        p = Proposal(id=self.next_proposal_id, **kw)
        self.proposals[p.id] = p
        self.next_proposal_id += 1
        return p


def fmt(utts: list[Utterance]) -> str:
    return "\n".join(f"{u.speaker}: {u.text}" for u in utts)


def parse_transcript_event(evt: dict) -> tuple[str, str, str] | None:
    """Return (bot_id, speaker, text) for a finalized transcript.data event."""
    if evt.get("event") != "transcript.data":
        return None
    data = evt.get("data", {})
    inner = data.get("data", {})
    words = inner.get("words") or []
    text = " ".join(w.get("text", "") for w in words).strip()
    participant = inner.get("participant") or {}
    speaker = participant.get("name") or f"Speaker {participant.get('id', '?')}"
    bot_id = (data.get("bot") or {}).get("id", "")
    return (bot_id, speaker, text) if text else None


def parse_chat_event(evt: dict) -> tuple[str, str, str] | None:
    """Return (bot_id, sender, text) for participant_events.chat_message."""
    if evt.get("event") != "participant_events.chat_message":
        return None
    data = evt.get("data", {})
    inner = data.get("data", {})
    payload = inner.get("data") or {}
    text = (payload.get("text") or payload.get("message") or "").strip()
    sender = (inner.get("participant") or {}).get("name") or "Someone"
    bot_id = (data.get("bot") or {}).get("id", "")
    return (bot_id, sender, text) if text else None


CMD = re.compile(
    r"^\s*(?P<verb>ok|yes|si|sí|no|skip)\s*#?(?P<id>\d+)\s*$"
    r"|^\s*(?P<toggle>focus)\s+(?P<state>on|off)\s*$"
    r"|^\s*(?P<stats>stats|metrics)\s*$"
    r"|^\s*agenda\s*:\s*(?P<agenda>.+)$",
    re.IGNORECASE,
)


def parse_command(text: str) -> dict | None:
    """Chat commands: 'ok 3', 'no 3', 'focus off', 'stats', 'agenda: new topics'."""
    m = CMD.match(text)
    if not m:
        return None
    if m.group("verb"):
        approve = m.group("verb").lower() in {"ok", "yes", "si", "sí"}
        return {"cmd": "approve" if approve else "reject", "id": int(m.group("id"))}
    if m.group("stats"):
        return {"cmd": "stats"}
    if m.group("toggle"):
        return {"cmd": "focus", "on": m.group("state").lower() == "on"}
    return {"cmd": "agenda", "agenda": m.group("agenda").strip()}
