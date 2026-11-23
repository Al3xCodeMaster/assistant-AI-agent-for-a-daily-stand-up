"""FastAPI entrypoint.  Run:  uvicorn app:app --port 8000"""
import asyncio
import html
import json
import logging
import time
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

import config
import calendar_sync
import copilot
import metrics
import notify
import recall
import report

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("app")
app = FastAPI(title="Meet Copilot")


class StartMeeting(BaseModel):
    meeting_url: str = ""
    agenda: str
    bot_id: str | None = None  # DRY_RUN only: attach to a fake bot id for local simulation
    mode: Literal["general", "daily"] = "general"
    participants: list[str] = []  # daily only: expected attendees; wins over the calendar lookup
    title: str = ""  # meeting name, shown on the timer page and in the report


@app.post("/meetings")
async def start_meeting(body: StartMeeting):
    if config.DRY_RUN and body.bot_id:
        s = copilot.start_session(body.bot_id, body.agenda, body.mode, body.participants, body.meeting_url,
                                  body.title)
        return {"bot_id": s.bot_id, "dry_run": True}
    if not (config.RECALL_API_KEY and config.PUBLIC_URL and body.meeting_url):
        raise HTTPException(400, "Set RECALL_API_KEY and PUBLIC_URL, and pass meeting_url.")
    bot = await recall.create_bot(body.meeting_url, body.agenda)
    await notify.send_meeting_notice(body.title, body.meeting_url)  # team can join; bot waits for admission
    copilot.start_session(bot["id"], body.agenda, body.mode, body.participants, body.meeting_url, body.title)
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
        "mode": s.mode,
        "timekeeper": s.timekeeper.summary(time.time()) if s.timekeeper else None,
    }


@app.get("/meetings/{bot_id}/timer.json")
async def timer_state(bot_id: str):
    """Live talk-time state for the timer page (daily mode only)."""
    s = copilot.SESSIONS.get(bot_id)
    if not s or not s.timekeeper:
        raise HTTPException(404)
    return {**s.timekeeper.snapshot(time.time()), "title": s.title, "project": s.current_project,
            "projects": s.projects}


@app.get("/meetings/{bot_id}/timer", response_class=HTMLResponse)
async def timer_page(bot_id: str):
    """Page the bot shows as its camera: generated per meeting, with the meeting title filled in."""
    s = copilot.SESSIONS.get(bot_id)
    if not s:
        raise HTTPException(404)
    template = (Path(__file__).parent / "static" / "timer.html").read_text(encoding="utf-8")
    return HTMLResponse(template.replace("{{TITLE}}", html.escape(s.title or "Daily")))


@app.get("/meetings/{bot_id}/report")
async def meeting_report(bot_id: str):
    """Daily report for planning and retrospective: live while the meeting runs, saved file afterwards."""
    s = copilot.SESSIONS.get(bot_id)
    if s:
        return report.build_report(s)
    saved = report.load_report(bot_id)
    if saved is None:
        raise HTTPException(404)
    return saved


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


@app.get("/calendar/google/connect")
async def google_connect():
    """Start the Google OAuth flow. Open this URL once per Google account."""
    if not (config.GOOGLE_OAUTH_CLIENT_ID and config.GOOGLE_OAUTH_CLIENT_SECRET and config.PUBLIC_URL):
        raise HTTPException(400, "Set GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET and PUBLIC_URL.")
    return RedirectResponse(calendar_sync.auth_url())


@app.get("/calendar/google/callback")
async def google_callback(code: str = "", state: str = "", error: str = ""):
    if error or not calendar_sync.consume_state(state) or not code:
        raise HTTPException(400, "Google authorization failed or expired; start again from /calendar/google/connect")
    refresh_token = await calendar_sync.exchange_code(code)
    calendar_id = await calendar_sync.create_recall_calendar(refresh_token)
    calendar_sync.save_calendar(calendar_id)
    return {"ok": True, "calendar_id": calendar_id}


@app.post("/webhooks/recall-calendar")
async def recall_calendar_webhook(request: Request):
    raw = await request.body()
    try:
        recall.verify_request(config.RECALL_VERIFICATION_SECRET, dict(request.headers), raw)
    except ValueError as e:
        log.warning("Rejected calendar webhook: %s", e)
        raise HTTPException(401, "invalid signature")
    evt = json.loads(raw)
    log.info("calendar event %s for calendar %s", evt.get("event"), evt.get("data", {}).get("calendar_id"))
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
