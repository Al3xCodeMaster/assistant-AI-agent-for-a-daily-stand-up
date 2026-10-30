"""Stdlib-only tests for the core logic (Claude and Recall calls are stubbed).

Run:  python -m unittest discover -s tests
"""
import asyncio
import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent  # noqa: E402
import config  # noqa: E402
import copilot  # noqa: E402
import recall  # noqa: E402
from session import parse_chat_event, parse_command, parse_transcript_event  # noqa: E402

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
        self.assertIn("Proposal #1", self.chats[-1])
        await copilot.handle_chat(self.s, "Ana", "ok 1")
        await asyncio.sleep(0.05)  # let the spawned execution finish
        self.assertEqual(self.s.proposals[1].status, "done")
        self.assertIn("✅ #1: Created PROJ-42.", self.chats[-1])
        await copilot.handle_chat(self.s, "Ana", "ok 1")
        self.assertIn("No pending proposal", self.chats[-1])

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
        self.assertTrue(any("Focus check" in c for c in self.chats))
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
        self.assertIn("in-topic", self.chats[-1])

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

    def test_mcp_block_pairing(self):
        from types import SimpleNamespace as B
        content = [
            B(type="mcp_tool_listing"),
            B(type="mcp_tool_use", id="u1", name="create_issue", server_name="jira"),
            B(type="mcp_tool_result", tool_use_id="u1", is_error=False),
            B(type="mcp_tool_use", id="u2", name="post_message", server_name="slack"),
            B(type="mcp_tool_result", tool_use_id="u2", is_error=True),
            B(type="text", text="Done"),
        ]
        self.assertEqual(agent.mcp_tool_calls(content), [
            {"server": "jira", "tool": "create_issue", "is_error": False},
            {"server": "slack", "tool": "post_message", "is_error": True},
        ])


if __name__ == "__main__":
    unittest.main()
