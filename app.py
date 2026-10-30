"""FastAPI entrypoint.  Run:  uvicorn app:app --port 8000"""
import asyncio
import json
import logging

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

import config
import copilot
import metrics
import recall

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("app")
app = FastAPI(title="Meet Copilot")


class StartMeeting(BaseModel):
    meeting_url: str = ""
    agenda: str
    bot_id: str | None = None  # DRY_RUN only: attach to a fake bot id for local simulation


@app.post("/meetings")
async def start_meeting(body: StartMeeting):
    if config.DRY_RUN and body.bot_id:
        s = copilot.start_session(body.bot_id, body.agenda)
        return {"bot_id": s.bot_id, "dry_run": True}
    if not (config.RECALL_API_KEY and config.PUBLIC_URL and body.meeting_url):
        raise HTTPException(400, "Set RECALL_API_KEY and PUBLIC_URL, and pass meeting_url.")
    bot = await recall.create_bot(body.meeting_url, body.agenda)
    copilot.start_session(bot["id"], body.agenda)
    return {"bot_id": bot["id"]}


@app.get("/meetings/{bot_id}")
async def meeting_state(bot_id: str):
    s = copilot.SESSIONS.get(bot_id)
    if not s:
        raise HTTPException(404)
    return {
        "agenda": s.agenda,
        "current_topic": s.current_topic,
        "focus_enabled": s.focus_enabled,
        "utterances": len(s.utterances),
        "last_lines": [f"{u.speaker}: {u.text}" for u in s.utterances[-10:]],
        "proposals": [vars(p) for p in s.proposals.values()],
    }


@app.get("/meetings/{bot_id}/metrics")
async def meeting_metrics(bot_id: str):
    """Live metrics for a meeting in progress, or the stored summary of a finished one."""
    s = copilot.SESSIONS.get(bot_id)
    if s:
        return s.metrics.summary()
    for row in reversed(metrics.load_history()):
        if row["bot_id"] == bot_id:
            return row
    raise HTTPException(404)


@app.get("/metrics")
async def all_metrics():
    """Totals across finished meetings (from metrics/meetings.jsonl) plus live ones."""
    finished = metrics.load_history()
    done_ids = {r["bot_id"] for r in finished}
    live = [s.metrics.summary() for s in copilot.SESSIONS.values()
            if s.metrics.started_at and s.bot_id not in done_ids]
    rows = finished + live
    total_s = sum(r["meeting_seconds"] for r in rows)
    on_s = sum(r["focus"]["seconds"]["on_topic"] for r in rows)
    return {
        "meetings": len(rows),
        "meeting_minutes": round(total_s / 60, 1),
        "in_topic_pct": round(100 * on_s / total_s, 1) if total_s else 0.0,  # time-weighted
        "mcp_executions": sum(r["mcp"]["executions"] for r in rows),
        "mcp_executions_ok": sum(r["mcp"]["executions_ok"] for r in rows),
        "mcp_tool_calls": sum(r["mcp"]["tool_calls"] for r in rows),
        "per_meeting": [
            {"bot_id": r["bot_id"], "status": r["status"], "minutes": round(r["meeting_seconds"] / 60, 1),
             "in_topic_pct": r["focus"]["in_topic_pct"], "mcp_executions": r["mcp"]["executions"]}
            for r in rows
        ],
    }


@app.post("/meetings/{bot_id}/leave")
async def leave(bot_id: str):
    await recall.leave_call(bot_id)
    copilot.end_session(bot_id)
    return {"ok": True}


@app.post("/webhooks/recall")
async def recall_webhook(request: Request):
    raw = await request.body()
    try:
        recall.verify_request(config.RECALL_VERIFICATION_SECRET, dict(request.headers), raw)
    except ValueError as e:
        log.warning("Rejected webhook: %s", e)
        raise HTTPException(401, "invalid signature")
    evt = json.loads(raw)
    # Acknowledge immediately; Recall sends transcript webhooks serially, so a slow
    # handler here would delay every utterance that follows.
    asyncio.create_task(copilot.handle_event(evt))
    return {"ok": True}
