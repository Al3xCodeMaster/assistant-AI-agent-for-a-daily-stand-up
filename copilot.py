"""Orchestration: routes Recall events, runs the per-meeting detect/focus loop."""
import asyncio
import datetime as dt
import logging
import time

import agent
import config
import meet_notes
import recall
from session import (
    MeetingSession, Proposal, fmt, is_voice_approver, parse_chat_event, parse_command, parse_speech_event,
    parse_transcript_duration, parse_transcript_event, parse_voice_approval,
)
from report import save_report
from timekeeper import Timekeeper

log = logging.getLogger("copilot")

SESSIONS: dict[str, MeetingSession] = {}
_LOOPS: dict[str, asyncio.Task] = {}
_BG: set[asyncio.Task] = set()  # keep references so tasks aren't garbage-collected


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _BG.add(t)
    t.add_done_callback(_BG.discard)


def start_session(bot_id: str, agenda: str, mode: str = "general",
                  participants: list[str] | None = None, meeting_url: str = "", title: str = "") -> MeetingSession:
    s = SESSIONS.get(bot_id)
    if s is None:
        s = MeetingSession(bot_id=bot_id, agenda=agenda, title=title)
        if mode == "daily":
            s.mode = "daily"
            s.timekeeper = Timekeeper(total_s=config.DAILY_TOTAL_S, min_turn_s=config.DAILY_MIN_TURN_S,
                                      expected=list(participants or []))
            if meeting_url and not participants:  # a manual list wins over the calendar
                _spawn(_load_attendees(s, meeting_url))
            if config.OPENPROJECT_PROJECT_ID:
                _spawn(_load_blocked(s))
    SESSIONS[bot_id] = s
    if bot_id not in _LOOPS:
        _LOOPS[bot_id] = asyncio.create_task(_loop(s))
    return s


async def _load_attendees(s: MeetingSession, meeting_url: str) -> None:
    attendees = await agent.fetch_attendees(meeting_url)
    if attendees and s.timekeeper and not s.timekeeper.expected:
        s.timekeeper.expected = attendees


async def _load_blocked(s: MeetingSession) -> None:
    """Blocked HU of the OpenProject project become the daily's context and are announced in chat."""
    blocked = await agent.fetch_blocked(config.OPENPROJECT_PROJECT_ID)
    if not blocked:
        return
    s.blocked = blocked
    names = "; ".join(f"#{b['id']} {b['subject']}" for b in blocked[:5])
    more = f" (+{len(blocked) - 5} más)" if len(blocked) > 5 else ""
    await recall.send_chat(s.bot_id, f"🚧 HU bloqueadas: {names}{more}")


def end_session(bot_id: str) -> None:
    s = SESSIONS.get(bot_id)
    if s:
        s.ended = True
        s.metrics.end()
        s.metrics.persist()
        save_report(s)
        if s.mode == "daily":
            _spawn(_finish_daily(s))
    task = _LOOPS.pop(bot_id, None)
    if task:
        task.cancel()


async def _finish_daily(s: MeetingSession) -> None:
    """After the daily: read Gemini notes (they appear minutes later), plan unblocks, post results to OpenProject."""
    if config.GOOGLE_REFRESH_TOKEN_NOTES:
        start_ts = s.metrics.started_at or (s.utterances[0].t if s.utterances else time.time())
        s.meet_notes = await meet_notes.wait_for_notes(
            config.GOOGLE_OAUTH_CLIENT_ID, config.GOOGLE_OAUTH_CLIENT_SECRET, config.GOOGLE_REFRESH_TOKEN_NOTES,
            dt.datetime.fromtimestamp(start_ts, dt.timezone.utc), s.title,
            config.MEET_NOTES_WAIT_S, config.MEET_NOTES_POLL_S,
        )
    if s.blocked:
        try:
            s.unblock_plan = await agent.plan_unblock(s.blocked, fmt(s.utterances[-300:]), s.meet_notes)
        except Exception:  # noqa: BLE001
            log.exception("plan_unblock failed")
            s.unblock_plan = []
        if config.DRY_RUN:
            log.info("DRY_RUN: would post %d unblock plans to OpenProject", len(s.unblock_plan))
        else:
            await _post_unblock_plans(s)
    if config.OPENPROJECT_DAILY_WP_ID and not config.DRY_RUN:
        await _post_daily_metrics(s)
    save_report(s)  # again, now with notes and plans


async def _post_unblock_plans(s: MeetingSession) -> None:
    blocked_ids = {str(b["id"]) for b in s.blocked}
    for plan in s.unblock_plan:
        hu_id = str(plan.get("hu_id", ""))
        if hu_id not in blocked_ids:  # only comment on HU that were actually blocked in this daily
            continue
        steps = "\n".join(f"{i}. {step}" for i, step in enumerate(plan.get("steps", []), 1))
        text = f"Plan de desbloqueo (daily {s.title or s.bot_id})\nResponsable: {plan.get('owner', 'por definir')}\n{steps}"
        try:
            await agent.openproject_call("add_comment", {"work_package_id": hu_id, "text": text})
        except Exception:  # noqa: BLE001 - one HU failing must not stop the others
            log.exception("add_comment on HU %s failed", hu_id)


def daily_metrics_text(s: MeetingSession) -> str:
    """Daily comment for OpenProject: duration, talk time per person and proposal outcomes.
    Focus is not measured in daily mode, so no focus percentages here."""
    minutes = s.metrics.summary()["meeting_seconds"] / 60
    lines = [f"Métricas daily {s.title or s.bot_id} ({config.BOT_NAME})", f"Duración: {minutes:.0f} min."]
    if s.timekeeper:
        tk = s.timekeeper.summary(time.time())
        lines.append(f"Tiempo hablado: {tk['talk_s'] / 60:.1f} min. Presupuesto por persona: "
                     f"{tk['budget_per_person_s'] / 60:.1f} min.")
        spoken = tk["spoken_s"]
        for name in sorted(set(tk["expected"]) | set(spoken), key=lambda n: -spoken.get(n, 0.0)):
            lines.append(f"- {name}: {spoken.get(name, 0.0) / 60:.1f} min")
    done = sum(1 for p in s.proposals.values() if p.status == "done")
    failed = sum(1 for p in s.proposals.values() if p.status == "failed")
    lines.append(f"Propuestas: {done} hechas, {failed} fallidas. HU con plan de desbloqueo: {len(s.unblock_plan)}.")
    return "\n".join(lines)


async def _post_daily_metrics(s: MeetingSession) -> None:
    text = daily_metrics_text(s)
    try:
        await agent.openproject_call("add_comment",
                                     {"work_package_id": config.OPENPROJECT_DAILY_WP_ID, "text": text})
    except Exception:  # noqa: BLE001
        log.exception("posting daily metrics to OpenProject failed")


# ---------------------------------------------------------------------------
# Event routing (called from the webhook handler, after verification)
# ---------------------------------------------------------------------------
async def handle_event(evt: dict) -> None:
    name = evt.get("event", "")

    if (sp := parse_speech_event(evt)) is not None:
        bot_id, speaker, kind = sp
        s = SESSIONS.get(bot_id)
        if s and s.timekeeper:
            s.speech_events_seen = True
            if kind == "on":
                s.timekeeper.speech_on(speaker, time.time())
            else:
                s.timekeeper.speech_off(speaker, time.time())
        return

    if (t := parse_transcript_event(evt)) is not None:
        bot_id, speaker, text = t
        s = SESSIONS.get(bot_id)
        if s:
            s.metrics.start()  # fallback if bot.in_call_recording wasn't received
            s.add(speaker, text)
            await _maybe_voice_approve(s, speaker, text)
            if s.timekeeper and not s.speech_events_seen and (secs := parse_transcript_duration(evt)):
                s.timekeeper.record(speaker, secs, time.time())
        return

    if (c := parse_chat_event(evt)) is not None:
        bot_id, sender, text = c
        s = SESSIONS.get(bot_id)
        if s and not text.startswith(config.CHAT_PREFIX):
            await handle_chat(s, sender, text)
        return

    bot_id = (evt.get("data", {}).get("bot") or {}).get("id", "")
    if name == "bot.in_call_recording" and bot_id in SESSIONS:
        s = SESSIONS[bot_id]
        s.metrics.start()  # meeting clock starts when recording starts
        if s.mode == "daily" and config.PUBLIC_URL:
            # the timer page becomes the bot's camera, visible to everyone in the meeting
            _spawn(recall.start_output_media(bot_id, f"{config.PUBLIC_URL}/meetings/{bot_id}/timer"))
    elif name in {"bot.done", "bot.call_ended", "bot.fatal"}:
        end_session(bot_id)


async def _maybe_voice_approve(s: MeetingSession, speaker: str, text: str) -> None:
    """A spoken approval by a listed approver runs that pending proposal, with a confirmation in chat.
    Only pending proposals run, and the status is set before the task starts, so one phrase runs once."""
    if not is_voice_approver(speaker, config.VOICE_APPROVERS):
        return
    n = parse_voice_approval(text)
    if n is None:
        return
    p = s.proposals.get(n)
    if not p or p.status != "pending":
        return
    p.status = "approved"  # claim it now: run_proposal runs later as a task
    log.info("%s approved #%d by voice", speaker, p.id)
    await recall.send_chat(s.bot_id, f"✅ #{p.id} aprobada por voz de {speaker}. Ejecutando...")
    _spawn(run_proposal(s, p))


async def handle_chat(s: MeetingSession, sender: str, text: str) -> None:
    cmd = parse_command(text)
    if not cmd:
        return
    if cmd["cmd"] in {"approve", "reject"}:
        p = s.proposals.get(cmd["id"])
        if not p or p.status != "pending":
            await recall.send_chat(s.bot_id, f"No hay propuesta pendiente #{cmd['id']}.")
            return
        if cmd["cmd"] == "reject":
            p.status = "rejected"
            await recall.send_chat(s.bot_id, f"Descartada #{p.id}.")
        else:
            log.info("%s approved #%d", sender, p.id)
            _spawn(run_proposal(s, p))
    elif cmd["cmd"] == "stats":
        await recall.send_chat(s.bot_id, s.metrics.chat_line())
    elif cmd["cmd"] == "focus":
        s.focus_enabled = cmd["on"]
        s.drift_strikes = 0
        s.metrics.reset_mark()  # time with alerts off stays unclassified
        await recall.send_chat(s.bot_id, f"Alertas de foco {'activadas' if cmd['on'] else 'desactivadas'}.")
    elif cmd["cmd"] == "agenda":
        s.agenda = cmd["agenda"]
        s.drift_strikes = 0
        await recall.send_chat(s.bot_id, f"Agenda actualizada: {s.agenda}")
    elif cmd["cmd"] == "timer":
        line = s.timekeeper.status_line(time.time()) if s.timekeeper else "Modo daily no activo."
        await recall.send_chat(s.bot_id, line)
    elif cmd["cmd"] == "participants":
        if not s.timekeeper:
            await recall.send_chat(s.bot_id, "Modo daily no activo.")
            return
        s.timekeeper.expected = cmd["names"]  # budget is recomputed from the new count
        await recall.send_chat(s.bot_id, f"Participantes: {', '.join(cmd['names'])}. "
                                         + s.timekeeper.status_line(time.time()))


async def run_proposal(s: MeetingSession, p: Proposal) -> None:
    p.status = "approved"
    t0 = time.time()
    try:
        p.result, calls = await agent.execute_action(s, p)
    except Exception as e:  # noqa: BLE001 - surface any tool/API failure in chat
        p.status, p.result = "failed", str(e)
        log.exception("Action #%d failed", p.id)
        s.metrics.record_execution(p.id, p.summary, p.server, False, [], time.time() - t0, error=str(e))
        await recall.send_chat(s.bot_id, f"⚠️ #{p.id} falló: {str(e)[:200]}")
        return
    # Claude answered, but it only counts as done if at least one MCP tool ran and none failed.
    ok = bool(calls) and not any(c["is_error"] for c in calls)
    p.status = "done" if ok else "failed"
    s.metrics.record_execution(p.id, p.summary, p.server, ok, calls, time.time() - t0,
                               error="" if ok else ("no MCP tool was called" if not calls else "MCP tool error"))
    await recall.send_chat(s.bot_id, f"{'✅' if ok else '⚠️'} #{p.id}: {p.result}")


# ---------------------------------------------------------------------------
# Periodic work
# ---------------------------------------------------------------------------
async def tick(s: MeetingSession, now: float | None = None) -> None:
    now = now or time.time()

    # 1) Action detection on utterances we haven't scanned yet
    if len(s.utterances) > s.detect_cursor and now - s.last_detect_at >= config.DETECT_INTERVAL_S:
        start, end = s.detect_cursor, len(s.utterances)
        s.detect_cursor, s.last_detect_at = end, now
        try:
            proposals = await agent.detect_actions(
                s, s.utterances[start:end], s.utterances[max(0, start - 20):start]
            )
        except Exception:  # noqa: BLE001
            log.exception("detect_actions failed")
            proposals = []
        for p in proposals:
            auto_status = config.AUTO_EXECUTE_STATUS and p.kind == "status_change" and p.server == "openproject"
            if config.AUTO_EXECUTE or auto_status:
                if auto_status:
                    await recall.send_chat(s.bot_id, f"🔄 Cambio de estado en OpenProject: {p.summary}")
                _spawn(run_proposal(s, p))
            else:
                await recall.send_chat(
                    s.bot_id, f"Propuesta #{p.id}: {p.summary} — responde 'ok {p.id}' o 'no {p.id}'"
                )

    # 2) Daily mode: time warnings replace focus checks entirely
    if s.timekeeper:
        s.timekeeper.tick(now)
        for msg in s.timekeeper.due_messages():
            # firm warnings (⚠️) are pinned so they stay visible in the chat
            await recall.send_chat(s.bot_id, msg, pin=msg.startswith("Se pasó"))

    # 3) Project in discussion, re-labeled every PROJECT_INTERVAL_S from the new transcript
    if s.mode == "daily" and len(s.utterances) > s.project_cursor and now - s.last_project_at >= config.PROJECT_INTERVAL_S:
        start = max(s.project_cursor, len(s.utterances) - 60)
        s.project_cursor, s.last_project_at = len(s.utterances), now
        try:
            project = await agent.detect_project(fmt(s.utterances[start:]), s.projects)
        except Exception:  # noqa: BLE001
            log.exception("detect_project failed")
            project = ""
        if project and project != s.current_project:
            s.current_project = project
            if project not in s.projects:
                s.projects.append(project)
            await recall.send_chat(s.bot_id, f"📌 Proyecto en discusión: {project}")

    # 4) Focus check on the rolling window (not in daily mode: drift is normal there)
    if s.mode != "daily" and s.focus_enabled and now - s.last_focus_at >= config.FOCUS_INTERVAL_S:
        s.last_focus_at = now
        window = s.window(config.WINDOW_S)
        if sum(len(u.text.split()) for u in window) < config.MIN_WINDOW_WORDS:
            s.metrics.record_focus(None, now)  # silence: segment stays unclassified
            return
        try:
            res = await agent.check_focus(s, window)
        except Exception:  # noqa: BLE001
            log.exception("check_focus failed")
            s.metrics.record_focus(None, now)
            return
        s.current_topic = res.get("current_topic", "")
        s.metrics.record_focus(res.get("status"), now, topic=s.current_topic)
        if res.get("status") == "on_topic":
            s.drift_strikes = 0
            return
        s.drift_strikes += 1
        if s.drift_strikes >= config.DRIFT_STRIKES and now - s.last_warning_at >= config.WARN_COOLDOWN_S:
            s.last_warning_at, s.drift_strikes = now, 0
            await recall.send_chat(
                s.bot_id, f"⏱️ Control de foco: {res.get('reason', '')} {res.get('suggestion', '')}".strip()
            )


async def _loop(s: MeetingSession) -> None:
    while not s.ended:
        try:
            await tick(s)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never let the loop die
            log.exception("tick failed")
        await asyncio.sleep(1)
