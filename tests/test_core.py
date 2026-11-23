"""Stdlib-only tests for the core logic (Claude and Recall calls are stubbed).

Run:  python -m unittest discover -s tests
"""
import asyncio
from pathlib import Path
import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent  # noqa: E402
import config  # noqa: E402
import copilot  # noqa: E402
import recall  # noqa: E402
from session import MeetingSession, parse_chat_event, parse_command, parse_speech_event, parse_transcript_duration, parse_transcript_event  # noqa: E402
from timekeeper import Timekeeper  # noqa: E402

SECRET = "whsec_" + base64.b64encode(b"test-secret-key-123456").decode()


class VerifyTests(unittest.TestCase):
    def test_roundtrip_and_tamper(self):
        body = b'{"event":"transcript.data"}'
        sig = recall.sign_request(SECRET, "msg_1", "1700000000", body)
        headers = {"Webhook-Id": "msg_1", "Webhook-Timestamp": "1700000000", "Webhook-Signature": sig}
        recall.verify_request(SECRET, headers, body)  # no exception
        with self.assertRaises(ValueError):
            recall.verify_request(SECRET, headers, body + b" ")
        # rotated secrets: several signatures, one valid
        headers["Webhook-Signature"] = "v1,AAAA " + sig
        recall.verify_request(SECRET, headers, body)

    def test_missing_headers(self):
        with self.assertRaises(ValueError):
            recall.verify_request(SECRET, {}, b"{}")


class ParseTests(unittest.TestCase):
    def test_transcript(self):
        evt = {"event": "transcript.data", "data": {"data": {
            "words": [{"text": "hello"}, {"text": "team"}], "participant": {"id": 7, "name": "Ana"}},
            "bot": {"id": "b1"}}}
        self.assertEqual(parse_transcript_event(evt), ("b1", "Ana", "hello team"))

    def test_chat(self):
        evt = {"event": "participant_events.chat_message", "data": {"data": {
            "participant": {"name": "Ana"}, "data": {"text": "ok 2"}}, "bot": {"id": "b1"}}}
        self.assertEqual(parse_chat_event(evt), ("b1", "Ana", "ok 2"))

    def test_commands(self):
        self.assertEqual(parse_command("ok 3"), {"cmd": "approve", "id": 3})
        self.assertEqual(parse_command("Sí #4"), {"cmd": "approve", "id": 4})
        self.assertEqual(parse_command("no 2"), {"cmd": "reject", "id": 2})
        self.assertEqual(parse_command("focus off"), {"cmd": "focus", "on": False})
        self.assertEqual(parse_command("agenda: budget, roadmap"), {"cmd": "agenda", "agenda": "budget, roadmap"})
        self.assertIsNone(parse_command("ok let's move on"))


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.chats = []

        async def fake_chat(bot_id, msg, pin=False):
            self.chats.append(msg)

        async def fake_detect(s, new, ctx):
            if any("ticket" in u.text for u in new):
                return [s.new_proposal(summary="Create Jira ticket: login crash (Luis, Fri)",
                                       instructions="...", server="jira", evidence="open a ticket")]
            return []

        self.focus_status = "off_topic"

        async def fake_focus(s, window):
            return {"status": self.focus_status, "current_topic": "football",
                    "reason": "Talking about football.", "suggestion": "Back to pricing?"}

        self.exec_calls = [{"server": "jira", "tool": "create_issue", "is_error": False}]

        async def fake_exec(s, p):
            return "Created PROJ-42.", self.exec_calls

        recall.send_chat, agent.detect_actions = fake_chat, fake_detect
        agent.check_focus, agent.execute_action = fake_focus, fake_exec
        config.AUTO_EXECUTE = False
        config.DETECT_INTERVAL_S, config.FOCUS_INTERVAL_S = 15, 45
        copilot.SESSIONS.clear()
        self.s = copilot.MeetingSession(bot_id="b1", agenda="release, pricing")
        copilot.SESSIONS["b1"] = self.s

    async def test_propose_approve_execute(self):
        self.s.add("Ana", "Luis can you open a ticket for the crash")
        await copilot.tick(self.s, now=1000)
        self.assertIn("Propuesta #1", self.chats[-1])
        await copilot.handle_chat(self.s, "Ana", "ok 1")
        await asyncio.sleep(0.05)  # let the spawned execution finish
        self.assertEqual(self.s.proposals[1].status, "done")
        self.assertIn("✅ #1: Created PROJ-42.", self.chats[-1])
        await copilot.handle_chat(self.s, "Ana", "ok 1")
        self.assertIn("No hay propuesta pendiente", self.chats[-1])

    async def test_focus_needs_two_strikes_and_cooldown(self):
        for i in range(10):
            self.s.add("Carla", "did you watch the match last night the ending was crazy " * 1)
        config.FOCUS_INTERVAL_S, config.DETECT_INTERVAL_S = 45, 10_000
        self.s.last_detect_at = 10**9  # disable detection here
        import time
        t0 = time.time()
        await copilot.tick(self.s, now=t0)        # strike 1, no warning
        self.assertFalse(any("Focus" in c for c in self.chats))
        await copilot.tick(self.s, now=t0 + 50)   # strike 2 → warning
        self.assertTrue(any("Control de foco" in c for c in self.chats))
        n = len(self.chats)
        await copilot.tick(self.s, now=t0 + 100)
        await copilot.tick(self.s, now=t0 + 150)  # still inside cooldown → silent
        self.assertEqual(len(self.chats), n)

    async def test_execution_metrics(self):
        self.s.add("Ana", "Luis can you open a ticket for the crash")
        await copilot.tick(self.s, now=1000)
        await copilot.handle_chat(self.s, "Ana", "ok 1")
        await asyncio.sleep(0.05)
        # second action: Claude answered but the MCP tool reported an error
        self.exec_calls = [{"server": "jira", "tool": "create_issue", "is_error": True}]
        p2 = self.s.new_proposal(summary="x", instructions="x", server="jira", evidence="x")
        await copilot.run_proposal(self.s, p2)
        # third action: Claude never called a tool
        self.exec_calls = []
        p3 = self.s.new_proposal(summary="y", instructions="y", server="jira", evidence="y")
        await copilot.run_proposal(self.s, p3)
        m = self.s.metrics.summary()["mcp"]
        self.assertEqual((m["executions"], m["executions_ok"], m["executions_failed"]), (3, 1, 2))
        self.assertEqual((m["tool_calls"], m["tool_calls_failed"]), (2, 1))
        self.assertEqual(m["tool_calls_by_server"], {"jira": 2})
        self.assertEqual(p3.status, "failed")

    async def test_stats_command(self):
        self.s.metrics.start()
        await copilot.handle_chat(self.s, "Ana", "stats")
        self.assertIn("en tema", self.chats[-1])

    async def test_bot_messages_ignored(self):
        evt = {"event": "participant_events.chat_message", "data": {"data": {
            "participant": {"name": "Copilot"}, "data": {"text": f"{config.CHAT_PREFIX} ok 1"}}, "bot": {"id": "b1"}}}
        await copilot.handle_event(evt)
        self.assertEqual(self.chats, [])


class MetricsTests(unittest.TestCase):
    def setUp(self):
        config.WINDOW_S = 150

    def test_time_split_is_share_of_total_meeting(self):
        from metrics import MeetingMetrics
        m = MeetingMetrics("b1")
        m.start(now=0)
        m.record_focus("on_topic", now=45)    # 0-45 on topic
        m.record_focus("off_topic", now=90)   # 45-90 off topic
        m.record_focus(None, now=135)         # 90-135 silence → unclassified
        m.record_focus("drifting", now=180)   # 135-180 drifting
        m.end(now=200)                         # 180-200 tail → unclassified
        f = m.summary()["focus"]
        self.assertEqual(m.summary()["meeting_seconds"], 200)
        self.assertEqual((f["in_topic_pct"], f["off_topic_pct"], f["drifting_pct"], f["unclassified_pct"]),
                         (22.5, 22.5, 22.5, 32.5))
        self.assertAlmostEqual(f["in_topic_pct"] + f["off_topic_pct"] + f["drifting_pct"] + f["unclassified_pct"], 100)
        self.assertAlmostEqual(f["in_topic_pct_of_classified"], 33.3)

    def test_long_gap_is_capped_at_window(self):
        from metrics import MeetingMetrics
        m = MeetingMetrics("b1")
        m.start(now=0)
        m.record_focus("on_topic", now=600)  # one check after 10 min only vouches for WINDOW_S
        self.assertEqual(m.seconds["on_topic"], 150)
        m.reset_mark(now=900)                # focus alerts turned back on
        m.record_focus("on_topic", now=945)
        self.assertEqual(m.seconds["on_topic"], 195)

    def test_ignored_before_start_and_after_end(self):
        from metrics import MeetingMetrics
        m = MeetingMetrics("b1")
        m.record_focus("on_topic", now=10)
        self.assertEqual(m.summary()["status"], "waiting")
        m.start(now=0); m.end(now=50); m.record_focus("on_topic", now=60)
        self.assertEqual(m.seconds["on_topic"], 0)

    def test_persist_and_history(self):
        import tempfile
        import metrics
        with tempfile.TemporaryDirectory() as d:
            config.METRICS_DIR = d
            m = metrics.MeetingMetrics("b9")
            m.start(now=0); m.record_focus("on_topic", now=60); m.end(now=120)
            self.assertIsNotNone(m.persist())
            self.assertIsNone(m.persist())  # only once
            rows = metrics.load_history()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["focus"]["in_topic_pct"], 50.0)

    def test_run_tools_loop_records_calls(self):
        """Gemini asks for an MCP tool, the app runs it, the model answers in text; errors are recorded."""
        import asyncio
        from types import SimpleNamespace as N
        from unittest import mock
        import llm
        import mcp_direct

        servers = [{"name": "jira", "url": "http://x", "authorization_token": ""},
                   {"name": "slack", "url": "http://y", "authorization_token": ""}]

        async def fake_list(url, token, http=None):
            name = "create_issue" if url == "http://x" else "post_message"
            return [{"name": name, "description": "d", "inputSchema": {"type": "object"}}]

        ran = []

        async def fake_call(url, token, tool, args, http=None):
            ran.append((tool, args))
            if tool == "post_message":
                return {"isError": True, "content": [{"type": "text", "text": "boom"}]}
            return {"isError": False, "content": [{"type": "text", "text": "ok"}]}

        responses = [
            N(function_calls=[N(name="jira__create_issue", args={"title": "x"}),
                              N(name="slack__post_message", args={"text": "hola"})],
              candidates=[N(content=N(role="model", parts=[]))], text=None),
            N(function_calls=None, candidates=[], text="Creé el ticket"),
        ]

        class FakeModels:
            async def generate_content(self, model, contents, config):
                return responses.pop(0)

        fake = N(aio=N(models=FakeModels()))
        with mock.patch.object(llm, "_client", fake), \
                mock.patch.object(mcp_direct, "list_tools", fake_list), \
                mock.patch.object(mcp_direct, "call_result", fake_call):
            text, calls = asyncio.run(llm.run_tools("m", "sys", "prompt", servers, 100))
        self.assertEqual(text, "Creé el ticket")
        self.assertEqual(ran, [("create_issue", {"title": "x"}), ("post_message", {"text": "hola"})])
        self.assertEqual(calls, [
            {"server": "jira", "tool": "create_issue", "is_error": False},
            {"server": "slack", "tool": "post_message", "is_error": True},
        ])

    def test_now_line_gives_today_in_team_zone(self):
        import datetime
        from zoneinfo import ZoneInfo
        from unittest import mock
        import agent
        import config

        with mock.patch.object(config, "TIMEZONE", "America/Bogota"):
            line = agent.now_line()
        today = datetime.datetime.now(ZoneInfo("America/Bogota")).strftime("%Y-%m-%d")
        self.assertIn(today, line)
        self.assertIn("America/Bogota", line)
        self.assertIn("RFC 3339", line)

    def test_forced_call_returns_arguments_of_named_tool(self):
        import asyncio
        from types import SimpleNamespace as N
        from unittest import mock
        import llm

        tool = {"name": "report_focus", "description": "d", "input_schema": {"type": "object"}}
        resp = N(function_calls=[N(name="report_focus", args={"status": "on_topic"})])

        class FakeModels:
            async def generate_content(self, model, contents, config):
                return resp

        with mock.patch.object(llm, "_client", N(aio=N(models=FakeModels()))):
            out = asyncio.run(llm.forced_call("m", "sys", "prompt", tool, 50))
        self.assertEqual(out, {"status": "on_topic"})


class DailyParseTests(unittest.TestCase):
    def transcript(self, words):
        return {"event": "transcript.data", "data": {"data": {"words": words, "participant": {"name": "Ana"}},
                                                     "bot": {"id": "b1"}}}

    def test_duration_from_word_timestamps(self):
        evt = self.transcript([
            {"text": "hi", "start_timestamp": {"relative": 10.0}, "end_timestamp": {"relative": 10.4}},
            {"text": "team", "start_timestamp": {"relative": 10.5}, "end_timestamp": {"relative": 12.0}},
        ])
        self.assertAlmostEqual(parse_transcript_duration(evt), 2.0)

    def test_duration_falls_back_to_word_count(self):
        evt = self.transcript([{"text": w} for w in "a b c d e".split()])
        self.assertAlmostEqual(parse_transcript_duration(evt), 2.0)  # 5 words at 2.5 per second

    def test_timer_command(self):
        self.assertEqual(parse_command("tiempo"), {"cmd": "timer"})
        self.assertEqual(parse_command("Timer"), {"cmd": "timer"})

    def test_participants_command(self):
        self.assertEqual(parse_command("participantes: Andres, Diego Fernando , Jhoni"),
                         {"cmd": "participants", "names": ["Andres", "Diego Fernando", "Jhoni"]})


class TimekeeperTests(unittest.TestCase):
    def make(self, total_s=300, expected=("Ana", "Luis", "Carla")):
        return Timekeeper(total_s=total_s, min_turn_s=30, expected=list(expected))

    def test_budget_is_total_over_expected(self):
        self.assertEqual(self.make().budget_s(), 100)

    def test_budget_uses_speakers_seen_and_respects_floor(self):
        tk = self.make(total_s=60, expected=())
        for name in ("A", "B", "C"):
            tk.speech_on(name, 0)
            tk.speech_off(name, 5)
        self.assertEqual(tk.budget_s(), 30)  # 60 / 3 = 20, floored to 30

    def test_warnings_fire_once_per_level_while_speaking(self):
        tk = self.make()  # budget 100
        tk.speech_on("Ana", 0)
        tk.tick(90)
        self.assertEqual(tk.due_messages(), [])
        tk.tick(105)  # 105 %: soft warning, while Ana is still talking
        msgs = tk.due_messages()
        self.assertEqual(len(msgs), 1)
        self.assertIn("Ana", msgs[0])
        tk.tick(125)  # still soft, nothing new
        self.assertEqual(tk.due_messages(), [])
        tk.tick(155)  # 155 %: firm warning
        msgs = tk.due_messages()
        self.assertEqual(len(msgs), 1)
        self.assertIn("parking lot", msgs[0])
        tk.speech_off("Ana", 200)  # closing the turn does not repeat the warning
        self.assertEqual(tk.due_messages(), [])

    def test_pauses_do_not_reset_the_turn(self):
        tk = self.make()  # budget 100
        tk.speech_on("Ana", 0)
        tk.speech_off("Ana", 60)  # short segment, then a pause
        tk.speech_on("Ana", 70)
        tk.tick(125)  # 60 + 55 = 115 s in the same turn
        msgs = tk.due_messages()
        self.assertEqual(len(msgs), 1)
        self.assertIn("Ana", msgs[0])

    def test_soft_warning_names_the_next_person_who_has_not_spoken(self):
        tk = self.make()
        tk.speech_on("Luis", 0)
        tk.speech_off("Luis", 10)  # Luis already spoke
        tk.speech_on("Ana", 10)
        tk.tick(120)  # Ana is over 100 s
        msgs = tk.due_messages()
        self.assertEqual(len(msgs), 1)
        self.assertIn("Pasamos a Carla", msgs[0])

    def test_new_speaker_gets_his_own_warning(self):
        tk = self.make()
        tk.speech_on("Ana", 0)
        tk.tick(120)
        self.assertEqual(len(tk.due_messages()), 1)
        tk.speech_off("Ana", 120)
        tk.speech_on("Luis", 120)
        tk.tick(225)  # Luis at 105 s
        msgs = tk.due_messages()
        self.assertEqual(len(msgs), 1)
        self.assertIn("Luis", msgs[0])

    def test_daily_total_warnings_fire_once(self):
        tk = self.make()  # total 300 s
        tk.speech_on("Ana", 0)
        tk.speech_off("Ana", 240)  # 80 % of the daily
        msgs = tk.due_messages()
        self.assertTrue(any("usados" in m for m in msgs))
        self.assertFalse(any("tiempo cumplido" in m for m in msgs))
        tk.speech_on("Luis", 240)
        tk.speech_off("Luis", 300)  # 100 %
        msgs = tk.due_messages()
        self.assertEqual(len(msgs), 1)
        self.assertIn("tiempo cumplido", msgs[0])
        self.assertEqual(tk.due_messages(), [])

    def test_transcript_fallback_record_still_works(self):
        tk = self.make()
        tk.record("Ana", 120, now=1000)
        self.assertEqual(len(tk.due_messages()), 1)

    def test_status_line_counts_open_turns(self):
        tk = self.make()
        tk.speech_on("Ana", 0)
        tk.speech_off("Ana", 50)
        tk.speech_on("Luis", 60)
        line = tk.status_line(now=70)
        self.assertIn("Ana", line)
        self.assertIn("Carla", line)
        self.assertIn("0:50/1:40", line)
        self.assertIn("0:10/1:40", line)  # Luis is still speaking
        self.assertEqual(tk.summary(now=70)["speaking_now"], ["Luis"])


class DailyFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.chats = []
        self.focus_calls = 0

        async def fake_chat(bot_id, msg, pin=False):
            self.chats.append(msg)

        async def fake_focus(s, window):
            self.focus_calls += 1
            return {"status": "off_topic", "current_topic": "x", "reason": "r", "suggestion": "s"}

        async def fake_detect(s, new, ctx):
            return []

        recall.send_chat, agent.check_focus, agent.detect_actions = fake_chat, fake_focus, fake_detect
        self.original_fetch = agent.fetch_attendees
        copilot.SESSIONS.clear()
        self.s = MeetingSession(bot_id="d1", agenda="daily", mode="daily")
        self.s.timekeeper = Timekeeper(total_s=300, min_turn_s=30, expected=[])
        copilot.SESSIONS["d1"] = self.s

    async def asyncTearDown(self):
        agent.fetch_attendees = self.original_fetch

    async def test_overrun_warns_and_focus_stays_silent(self):
        self.s.timekeeper.expected = ["Ana", "Luis"]  # budget 150 s per person
        self.s.timekeeper.speech_on("Ana", 800)  # speaking for 200 s at now=1000
        await copilot.tick(self.s, now=1000)  # 133 %: soft
        self.assertEqual(len(self.chats), 1)
        self.assertIn("cierra", self.chats[0])
        await copilot.tick(self.s, now=1100)  # 200 %: hard (the daily total also crosses 80 % and 100 % here)
        self.assertTrue(any("parking lot" in c for c in self.chats))
        n = len(self.chats)
        await copilot.tick(self.s, now=1200)  # nothing repeats
        self.assertEqual(len(self.chats), n)
        self.assertEqual(self.focus_calls, 0)

    async def test_speech_events_drive_the_timekeeper(self):
        import time
        on = {"event": "participant_events.speech_on", "data": {"data": {"participant": {"name": "Ana"}},
                                                                 "bot": {"id": "d1"}}}
        off = {"event": "participant_events.speech_off", "data": {"data": {"participant": {"name": "Ana"}},
                                                                   "bot": {"id": "d1"}}}
        await copilot.handle_event(on)
        self.assertIn("Ana", self.s.timekeeper.open_turns)
        time.sleep(0.01)
        await copilot.handle_event(off)
        self.assertNotIn("Ana", self.s.timekeeper.open_turns)
        self.assertIn("Ana", self.s.timekeeper.spoken)
        self.assertTrue(self.s.speech_events_seen)

    async def test_transcript_timing_ignored_once_speech_events_arrive(self):
        self.s.speech_events_seen = True
        evt = {"event": "transcript.data", "data": {"data": {"words": [
            {"text": "hola", "start_timestamp": {"relative": 0.0}, "end_timestamp": {"relative": 30.0}}],
            "participant": {"name": "Ana"}}, "bot": {"id": "d1"}}}
        await copilot.handle_event(evt)
        self.assertNotIn("Ana", self.s.timekeeper.spoken)

    async def test_participants_command_replaces_the_expected_list(self):
        await copilot.handle_chat(self.s, "Ana", "participantes: Diego Alcides Carabali, Jhoni")
        self.assertEqual(self.s.timekeeper.expected, ["Diego Alcides Carabali", "Jhoni"])
        self.assertIn("Jhoni", self.chats[-1])
        self.assertEqual(self.s.timekeeper.budget_s(), 150)  # 300 s / 2 people

    async def test_timer_command_replies_with_status(self):
        import time
        now = time.time()
        self.s.timekeeper.speech_on("Ana", now - 50)
        self.s.timekeeper.speech_off("Ana", now)
        await copilot.handle_chat(self.s, "Ana", "tiempo")
        self.assertIn("Ana", self.chats[-1])

    async def test_calendar_fills_expected_when_no_manual_list(self):
        async def fake_fetch(url):
            return ["Ana", "Luis"]

        agent.fetch_attendees = fake_fetch
        await copilot._load_attendees(self.s, "https://meet.google.com/abc")
        self.assertEqual(self.s.timekeeper.expected, ["Ana", "Luis"])

    async def test_manual_list_wins_over_calendar(self):
        async def fake_fetch(url):
            return ["Someone else"]

        agent.fetch_attendees = fake_fetch
        self.s.timekeeper.expected = ["Ana"]
        await copilot._load_attendees(self.s, "https://meet.google.com/abc")
        self.assertEqual(self.s.timekeeper.expected, ["Ana"])


class ProjectAndReportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.chats = []

        async def fake_chat(bot_id, msg, pin=False):
            self.chats.append(msg)

        async def fake_detect(s, new, ctx):
            return []

        async def fake_project(text, known):
            return "Proyecto Alfa"

        self.original_project = agent.detect_project
        recall.send_chat, agent.detect_actions, agent.detect_project = fake_chat, fake_detect, fake_project
        copilot.SESSIONS.clear()
        self.s = MeetingSession(bot_id="p1", agenda="daily", mode="daily", title="Daily Comfenalco")
        self.s.timekeeper = Timekeeper(total_s=300, min_turn_s=30, expected=["Ana"])
        copilot.SESSIONS["p1"] = self.s

    async def test_tick_labels_the_project_and_tells_the_meeting(self):
        self.s.add("Ana", "revisamos el avance del proyecto alfa con el cliente")
        await copilot.tick(self.s, now=1000)
        self.assertEqual(self.s.current_project, "Proyecto Alfa")
        self.assertEqual(self.s.projects, ["Proyecto Alfa"])
        self.assertIn("Proyecto en discusión: Proyecto Alfa", self.chats[-1])

    def test_report_has_title_projects_and_talk(self):
        from report import build_report
        self.s.projects = ["Proyecto Alfa"]
        rep = build_report(self.s, now=1000)
        self.assertEqual(rep["title"], "Daily Comfenalco")
        self.assertEqual(rep["projects"], ["Proyecto Alfa"])
        self.assertIn("talk", rep)

    async def asyncTearDown(self):
        agent.detect_project = self.original_project


class GoogleCalendarTests(unittest.TestCase):
    def test_auth_url_asks_for_offline_calendar_read_access(self):
        from urllib.parse import parse_qs, urlparse
        import calendar_sync
        original = config.GOOGLE_OAUTH_CLIENT_ID, config.PUBLIC_URL
        config.GOOGLE_OAUTH_CLIENT_ID, config.PUBLIC_URL = "cid", "https://example.ngrok-free.dev"
        try:
            q = parse_qs(urlparse(calendar_sync.auth_url()).query)
        finally:
            config.GOOGLE_OAUTH_CLIENT_ID, config.PUBLIC_URL = original
        self.assertEqual(q["client_id"], ["cid"])
        self.assertEqual(q["access_type"], ["offline"])
        self.assertEqual(q["prompt"], ["consent"])
        self.assertIn("calendar.readonly", q["scope"][0])
        self.assertEqual(q["redirect_uri"], ["https://example.ngrok-free.dev/calendar/google/callback"])

    def test_state_is_single_use(self):
        import calendar_sync
        calendar_sync.auth_url()
        state = next(iter(calendar_sync._pending_states))
        self.assertTrue(calendar_sync.consume_state(state))
        self.assertFalse(calendar_sync.consume_state(state))
        self.assertFalse(calendar_sync.consume_state("unknown"))

    def test_recall_body_has_no_google_token_fields_besides_refresh(self):
        import calendar_sync
        body = calendar_sync.recall_calendar_body("refresh-123")
        self.assertEqual(body["platform"], "google_calendar")
        self.assertEqual(body["oauth_refresh_token"], "refresh-123")

    def test_saves_only_the_recall_calendar_id(self):
        import calendar_sync, tempfile
        with tempfile.TemporaryDirectory() as d:
            original = config.CALENDAR_DB
            config.CALENDAR_DB = str(Path(d) / "calendars.json")
            try:
                calendar_sync.save_calendar("cal-1")
                rows = calendar_sync.load_calendars()
            finally:
                config.CALENDAR_DB = original
        self.assertEqual([r["calendar_id"] for r in rows], ["cal-1"])
        self.assertNotIn("refresh", json.dumps(rows))


class NotifyTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_has_title_and_link(self):
        from notify import build_message
        msg = build_message("Proyecto Alfa", "https://meet.google.com/abc-defg-hij")
        self.assertIn("Proyecto Alfa", msg)
        self.assertIn("https://meet.google.com/abc-defg-hij", msg)

    async def test_no_webhook_configured_is_a_noop(self):
        from notify import send_meeting_notice
        original, config.NOTIFY_WEBHOOK_URL = config.NOTIFY_WEBHOOK_URL, ""
        try:
            self.assertFalse(await send_meeting_notice("x", "https://meet.google.com/abc-defg-hij"))
        finally:
            config.NOTIFY_WEBHOOK_URL = original


class OutputMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_bot_shows_the_timer_page_when_it_joins(self):
        calls = []

        async def fake_output(bot_id, url):
            calls.append((bot_id, url))

        original, recall.start_output_media = recall.start_output_media, fake_output
        original_url, config.PUBLIC_URL = config.PUBLIC_URL, "https://example.ngrok-free.dev"
        try:
            copilot.SESSIONS.clear()
            s = MeetingSession(bot_id="o1", agenda="daily", mode="daily")
            s.timekeeper = Timekeeper(total_s=300, min_turn_s=30, expected=[])
            copilot.SESSIONS["o1"] = s
            evt = {"event": "bot.in_call_recording", "data": {"bot": {"id": "o1"}}}
            await copilot.handle_event(evt)
            await asyncio.sleep(0.01)  # let the spawned task run
        finally:
            recall.start_output_media = original
            config.PUBLIC_URL = original_url
        self.assertEqual(calls, [("o1", "https://example.ngrok-free.dev/meetings/o1/timer")])


class TimerPageTests(unittest.TestCase):
    def setUp(self):
        import time
        from fastapi.testclient import TestClient
        import app as app_module
        self.time = time
        self.client = TestClient(app_module.app)
        copilot.SESSIONS.clear()
        self.s = MeetingSession(bot_id="t1", agenda="daily", mode="daily")
        self.s.timekeeper = Timekeeper(total_s=300, min_turn_s=30, expected=["Ana", "Luis"])
        copilot.SESSIONS["t1"] = self.s

    def test_snapshot_lists_people_and_current_turn(self):
        self.s.timekeeper.speech_on("Ana", self.time.time() - 10)
        self.s.timekeeper.speech_off("Ana", self.time.time())
        self.s.timekeeper.speech_on("Ana", self.time.time())
        data = self.client.get("/meetings/t1/timer.json").json()
        self.assertEqual([p["name"] for p in data["people"]], ["Ana", "Luis"])
        self.assertTrue(data["people"][0]["speaking"])
        self.assertEqual(data["turn"]["speaker"], "Ana")
        self.assertEqual(data["budget_s"], 150.0)

    def test_page_shows_the_meeting_title(self):
        self.s.title = "Proyecto Alfa - Sprint 12"
        self.assertIn("Proyecto Alfa - Sprint 12", self.client.get("/meetings/t1/timer").text)

    def test_live_report_endpoint(self):
        self.s.title = "Daily Comfenalco"
        r = self.client.get("/meetings/t1/report")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["title"], "Daily Comfenalco")
        self.assertEqual(self.client.get("/meetings/nope/report").status_code, 404)

    def test_page_is_served_and_unknown_bot_is_404(self):
        r = self.client.get("/meetings/t1/timer")
        self.assertEqual(r.status_code, 200)
        self.assertIn("timer.json", r.text)
        self.assertEqual(self.client.get("/meetings/nope/timer").status_code, 404)
        self.assertEqual(self.client.get("/meetings/nope/timer.json").status_code, 404)

    def test_alert_is_visible_in_the_snapshot(self):
        self.s.timekeeper.speech_on("Ana", 0)
        self.s.timekeeper.tick(200)
        self.assertIn("Ana", self.client.get("/meetings/t1/timer.json").json()["alert"])


if __name__ == "__main__":
    unittest.main()
