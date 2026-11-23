"""Per-meeting state and parsing of Recall real-time events."""
import re
import time
from dataclasses import dataclass, field

from metrics import MeetingMetrics
from timekeeper import Timekeeper


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
    kind: str = "other"  # status_change = moves one existing work package to another status (may run without ok)


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
    mode: str = "general"  # general | daily
    timekeeper: Timekeeper | None = None  # only set in daily mode
    blocked: list[dict] = field(default_factory=list)  # open OpenProject HU blocked at the start (daily only)
    unblock_plan: list[dict] = field(default_factory=list)  # step-by-step plan per HU, built at the end
    meet_notes: str = ""  # Gemini notes text from Drive, when found
    speech_events_seen: bool = False  # once True, transcript timing is ignored to avoid counting twice
    title: str = ""  # name of the meeting, shown on the timer page and in the report
    current_project: str = ""  # project being discussed right now
    projects: list[str] = field(default_factory=list)  # every project named in this meeting, in order
    project_cursor: int = 0  # first utterance not yet used for project detection
    last_project_at: float = 0.0

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


_NUMBER_WORDS = {"uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
                 "ocho": 8, "nueve": 9, "diez": 10}
_OK_RE = re.compile(r"\bok\s+(\d+)\b")
_APPROVE_RE = re.compile(
    r"\b(?:aprueba|aprobar|apruebo|aprobada|aprobado)\b.*?\bpropuesta\s+(?:numero\s+|#)?(\d+|" +
    "|".join(_NUMBER_WORDS) + r")\b")


def _norm(text: str) -> str:
    """Lowercase and drop accents, so 'número' and 'numero' match the same pattern."""
    import unicodedata
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def parse_voice_approval(text: str) -> int | None:
    """Proposal number approved by a spoken line: 'ok 1' or 'aprueba la propuesta uno'. None otherwise."""
    t = _norm(text)
    if m := _OK_RE.search(t):
        return int(m.group(1))
    if m := _APPROVE_RE.search(t):
        raw = m.group(1)
        return int(raw) if raw.isdigit() else _NUMBER_WORDS[raw]
    return None


def is_voice_approver(speaker: str, approvers: list[str]) -> bool:
    """Speaker name as Meet shows it, compared without case or accents."""
    return bool(speaker) and _norm(speaker).strip() in {_norm(a).strip() for a in approvers}


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


def parse_speech_event(evt: dict) -> tuple[str, str, str] | None:
    """Return (bot_id, speaker, 'on' | 'off') for participant_events.speech_on / speech_off."""
    name = evt.get("event", "")
    if name not in {"participant_events.speech_on", "participant_events.speech_off"}:
        return None
    data = evt.get("data", {})
    speaker = ((data.get("data") or {}).get("participant") or {}).get("name") or ""
    bot_id = (data.get("bot") or {}).get("id", "")
    return (bot_id, speaker, "on" if name.endswith("speech_on") else "off") if speaker else None


def _rel(ts) -> float | None:
    """Recall word timestamps look like {"relative": 1.2, "absolute": "..."}; None if missing."""
    value = ts.get("relative") if isinstance(ts, dict) else None
    return float(value) if isinstance(value, (int, float)) else None


def parse_transcript_duration(evt: dict) -> float | None:
    """Speaking seconds of a transcript.data event: word timestamps when present, else ~2.5 words per second."""
    if evt.get("event") != "transcript.data":
        return None
    words = (evt.get("data", {}).get("data", {}).get("words")) or []
    if not words:
        return None
    spans = [(_rel(w.get("start_timestamp")), _rel(w.get("end_timestamp"))) for w in words]
    if all(start is not None and end is not None for start, end in spans):
        return max(0.0, spans[-1][1] - spans[0][0])
    return len(words) / 2.5


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
    r"|^\s*(?P<timer>timer|tiempo)\s*$"
    r"|^\s*(?P<people>participantes|participants)\s*:\s*(?P<names>.+)$"
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
    if m.group("timer"):
        return {"cmd": "timer"}
    if m.group("people"):
        names = [n.strip() for n in m.group("names").split(",") if n.strip()]
        return {"cmd": "participants", "names": names}
    return {"cmd": "agenda", "agenda": m.group("agenda").strip()}
