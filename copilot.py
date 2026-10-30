"""Orchestration: routes Recall events, runs the per-meeting detect/focus loop."""
import asyncio
import logging
import time

import agent
import config
import recall
from session import MeetingSession, Proposal, parse_chat_event, parse_command, parse_transcript_event

log = logging.getLogger("copilot")

SESSIONS: dict[str, MeetingSession] = {}
_LOOPS: dict[str, asyncio.Task] = {}
_BG: set[asyncio.Task] = set()  # keep references so tasks aren't garbage-collected


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _BG.add(t)
    t.add_done_callback(_BG.discard)


def start_session(bot_id: str, agenda: str) -> MeetingSession:
    s = SESSIONS.get(bot_id) or MeetingSession(bot_id=bot_id, agenda=agenda)
    SESSIONS[bot_id] = s
    if bot_id not in _LOOPS:
        _LOOPS[bot_id] = asyncio.create_task(_loop(s))
    return s


def end_session(bot_id: str) -> None:
    s = SESSIONS.get(bot_id)
    if s:
        s.ended = True
        s.metrics.end()
        s.metrics.persist()
    task = _LOOPS.pop(bot_id, None)
    if task:
        task.cancel()


# ---------------------------------------------------------------------------
# Event routing (called from the webhook handler, after verification)
# ---------------------------------------------------------------------------
async def handle_event(evt: dict) -> None:
    name = evt.get("event", "")

    if (t := parse_transcript_event(evt)) is not None:
        bot_id, speaker, text = t
        s = SESSIONS.get(bot_id)
        if s:
            s.metrics.start()  # fallback if bot.in_call_recording wasn't received
            s.add(speaker, text)
        return

    if (c := parse_chat_event(evt)) is not None:
        bot_id, sender, text = c
        s = SESSIONS.get(bot_id)
        if s and not text.startswith(config.CHAT_PREFIX):
            await handle_chat(s, sender, text)
        return

    bot_id = (evt.get("data", {}).get("bot") or {}).get("id", "")
    if name == "bot.in_call_recording" and bot_id in SESSIONS:
        SESSIONS[bot_id].metrics.start()  # meeting clock starts when recording starts
    elif name in {"bot.done", "bot.call_ended", "bot.fatal"}:
        end_session(bot_id)


async def handle_chat(s: MeetingSession, sender: str, text: str) -> None:
    cmd = parse_command(text)
    if not cmd:
        return
    if cmd["cmd"] in {"approve", "reject"}:
        p = s.proposals.get(cmd["id"])
        if not p or p.status != "pending":
            await recall.send_chat(s.bot_id, f"No pending proposal #{cmd['id']}.")
            return
        if cmd["cmd"] == "reject":
            p.status = "rejected"
            await recall.send_chat(s.bot_id, f"Skipped #{p.id}.")
        else:
            log.info("%s approved #%d", sender, p.id)
            _spawn(run_proposal(s, p))
    elif cmd["cmd"] == "stats":
        await recall.send_chat(s.bot_id, s.metrics.chat_line())
    elif cmd["cmd"] == "focus":
        s.focus_enabled = cmd["on"]
        s.drift_strikes = 0
        s.metrics.reset_mark()  # time with alerts off stays unclassified
        await recall.send_chat(s.bot_id, f"Focus alerts {'on' if cmd['on'] else 'off'}.")
    elif cmd["cmd"] == "agenda":
        s.agenda = cmd["agenda"]
        s.drift_strikes = 0
        await recall.send_chat(s.bot_id, f"Agenda updated: {s.agenda}")


async def run_proposal(s: MeetingSession, p: Proposal) -> None:
    p.status = "approved"
    t0 = time.time()
    try:
        p.result, calls = await agent.execute_action(s, p)
    except Exception as e:  # noqa: BLE001 - surface any tool/API failure in chat
        p.status, p.result = "failed", str(e)
        log.exception("Action #%d failed", p.id)
        s.metrics.record_execution(p.id, p.summary, p.server, False, [], time.time() - t0, error=str(e))
        await recall.send_chat(s.bot_id, f"⚠️ #{p.id} failed: {str(e)[:200]}")
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
            if config.AUTO_EXECUTE:
                _spawn(run_proposal(s, p))
            else:
                await recall.send_chat(
                    s.bot_id, f"Proposal #{p.id}: {p.summary} — reply 'ok {p.id}' or 'no {p.id}'"
                )

    # 2) Focus check on the rolling window
    if s.focus_enabled and now - s.last_focus_at >= config.FOCUS_INTERVAL_S:
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
                s.bot_id, f"⏱️ Focus check: {res.get('reason', '')} {res.get('suggestion', '')}".strip()
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
