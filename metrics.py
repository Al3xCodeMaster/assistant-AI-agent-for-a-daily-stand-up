"""Per-meeting metrics: on-topic time share and MCP executions.

Time accounting
---------------
The meeting clock starts when the bot is recording (first transcript or the
bot.in_call_recording event) and stops when the call ends. Each focus check
covers the time since the previous check (capped at WINDOW_S) and assigns it
to the status Claude returned: on_topic, drifting or off_topic. Time no check
covered (silence, focus alerts turned off, the tail after the last check) is
"unclassified". All percentages are of total meeting time, so they sum to 100.
"""
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import config

log = logging.getLogger("metrics")
STATUSES = ("on_topic", "drifting", "off_topic")


def _now(now: float | None) -> float:
    return time.time() if now is None else now


def _emit(event: str, **fields) -> None:
    """One JSON line per metric event, easy to ship to any log pipeline."""
    log.info(json.dumps({"metric_event": event, **fields}, ensure_ascii=False, default=str))


@dataclass
class MeetingMetrics:
    bot_id: str
    started_at: float | None = None
    ended_at: float | None = None
    seconds: dict[str, float] = field(default_factory=lambda: {s: 0.0 for s in STATUSES})
    _mark: float | None = None  # end of the last focus segment
    focus_checks: int = 0
    executions: list[dict] = field(default_factory=list)  # one entry per approved action run
    persisted: bool = False

    # --- meeting clock -------------------------------------------------
    def start(self, now: float | None = None) -> None:
        if self.started_at is None:
            self.started_at = self._mark = _now(now)
            _emit("meeting_started", bot_id=self.bot_id, at=self.started_at)

    def end(self, now: float | None = None) -> None:
        if self.started_at is not None and self.ended_at is None:
            self.ended_at = _now(now)

    def total_seconds(self, now: float | None = None) -> float:
        if self.started_at is None:
            return 0.0
        end = self.ended_at if self.ended_at is not None else _now(now)
        return max(0.0, end - self.started_at)

    # --- focus ---------------------------------------------------------
    def reset_mark(self, now: float | None = None) -> None:
        """Skip the segment since the last mark (e.g. focus alerts were off)."""
        if self.started_at is not None:
            self._mark = _now(now)

    def record_focus(self, status: str | None, now: float | None = None, topic: str = "") -> None:
        """status=None means the check was skipped (silence): segment stays unclassified."""
        now = _now(now)
        if self.started_at is None or self.ended_at is not None:
            return
        segment = max(0.0, now - (self._mark if self._mark is not None else now))
        self._mark = now
        if status not in STATUSES:
            return
        covered = min(segment, config.WINDOW_S)
        self.seconds[status] += covered
        self.focus_checks += 1
        _emit("focus_check", bot_id=self.bot_id, status=status, topic=topic, seconds=round(covered, 1))

    # --- MCP executions -------------------------------------------------
    def record_execution(self, proposal_id: int, summary: str, server: str, ok: bool,
                         tool_calls: list[dict], duration_s: float, error: str = "") -> None:
        entry = {
            "proposal_id": proposal_id,
            "summary": summary,
            "server": server,
            "ok": ok,
            "tool_calls": tool_calls,  # [{"server", "tool", "is_error"}]
            "duration_s": round(duration_s, 2),
            "at": time.time(),
        }
        if error:
            entry["error"] = error[:300]
        self.executions.append(entry)
        _emit("mcp_execution", bot_id=self.bot_id, **entry)

    # --- reporting -----------------------------------------------------
    def summary(self, now: float | None = None) -> dict:
        total = self.total_seconds(now)
        classified = sum(self.seconds.values())
        unclassified = max(0.0, total - classified)

        def pct(x: float) -> float:
            return round(100 * x / total, 1) if total else 0.0

        calls = [c for e in self.executions for c in e["tool_calls"]]
        by_server: dict[str, int] = {}
        for c in calls:
            by_server[c["server"]] = by_server.get(c["server"], 0) + 1
        return {
            "bot_id": self.bot_id,
            "status": "ended" if self.ended_at is not None else ("live" if self.started_at is not None else "waiting"),
            "meeting_seconds": round(total, 1),
            "focus": {
                "in_topic_pct": pct(self.seconds["on_topic"]),
                "drifting_pct": pct(self.seconds["drifting"]),
                "off_topic_pct": pct(self.seconds["off_topic"]),
                "unclassified_pct": pct(unclassified),
                "in_topic_pct_of_classified": round(100 * self.seconds["on_topic"] / classified, 1) if classified else None,
                "seconds": {**{k: round(v, 1) for k, v in self.seconds.items()}, "unclassified": round(unclassified, 1)},
                "checks": self.focus_checks,
            },
            "mcp": {
                "executions": len(self.executions),
                "executions_ok": sum(e["ok"] for e in self.executions),
                "executions_failed": sum(not e["ok"] for e in self.executions),
                "tool_calls": len(calls),
                "tool_calls_failed": sum(bool(c["is_error"]) for c in calls),
                "tool_calls_by_server": by_server,
                "log": self.executions,
            },
        }

    def chat_line(self, now: float | None = None) -> str:
        s = self.summary(now)
        f, m = s["focus"], s["mcp"]
        mins = s["meeting_seconds"] / 60
        return (f"Stats ({mins:.0f} min): {f['in_topic_pct']}% in-topic, {f['off_topic_pct'] + f['drifting_pct']:.1f}% "
                f"off/drifting, {f['unclassified_pct']}% unclassified. MCP: {m['executions_ok']} actions done "
                f"({m['tool_calls']} tool calls), {m['executions_failed']} failed.")

    def persist(self) -> Path | None:
        """Append the final summary to METRICS_DIR/meetings.jsonl (once per meeting)."""
        if self.persisted or self.started_at is None:
            return None
        self.end()
        out = Path(config.METRICS_DIR)
        out.mkdir(parents=True, exist_ok=True)
        path = out / "meetings.jsonl"
        summary = self.summary()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({**summary, "ended_at": self.ended_at}, ensure_ascii=False) + "\n")
        self.persisted = True
        _emit("meeting_ended", **{k: v for k, v in summary.items() if k != "mcp"},
              mcp={k: v for k, v in summary["mcp"].items() if k != "log"})
        return path


def load_history() -> list[dict]:
    path = Path(config.METRICS_DIR) / "meetings.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
