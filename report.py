"""End-of-meeting report for daily meetings: input for planning and retrospective.

Saved as METRICS_DIR/daily/<bot_id>.json. It holds per-person talk time, so keep it private.
"""
import json
import time
from pathlib import Path

import config


def build_report(s, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    return {
        "bot_id": s.bot_id,
        "title": s.title,
        "agenda": s.agenda,
        "mode": s.mode,
        "projects": s.projects,
        "current_project": s.current_project,
        "talk": s.timekeeper.summary(now) if s.timekeeper else None,
        "proposals": [{"id": p.id, "summary": p.summary, "status": p.status, "result": p.result}
                      for p in s.proposals.values()],
        "blocked_hu": s.blocked,
        "unblock_plan": s.unblock_plan,
        "meet_notes": s.meet_notes,
        "generated_at": now,
    }


def save_report(s, now: float | None = None) -> Path | None:
    if s.mode != "daily":
        return None
    folder = Path(config.METRICS_DIR) / "daily"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{s.bot_id}.json"
    path.write_text(json.dumps(build_report(s, now), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_report(bot_id: str) -> dict | None:
    path = Path(config.METRICS_DIR) / "daily" / f"{bot_id}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
