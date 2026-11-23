"""Tests for approval by voice: parsing spoken approvals and running a pending proposal once.

No Recall or Claude calls: the chat send and the proposal run are replaced.
Run:  python3 -m unittest tests.test_voice_approval
"""
import asyncio
import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

import copilot  # noqa: E402
from session import MeetingSession, is_voice_approver, parse_voice_approval  # noqa: E402


class ParseTests(unittest.TestCase):
    def test_spoken_approval_with_number_word(self):
        self.assertEqual(parse_voice_approval("Aprueba la propuesta uno."), 1)

    def test_spoken_approval_with_accent_and_digit(self):
        self.assertEqual(parse_voice_approval("Apruebo la propuesta número 2"), 2)

    def test_ok_with_number(self):
        self.assertEqual(parse_voice_approval("ok 3"), 3)

    def test_number_word_range(self):
        self.assertEqual(parse_voice_approval("aprobada la propuesta cinco"), 5)

    def test_no_approval_without_verb(self):
        self.assertIsNone(parse_voice_approval("la propuesta uno está bien"))

    def test_no_number_no_approval(self):
        self.assertIsNone(parse_voice_approval("aprueba la propuesta"))

    def test_normal_talk_ignored(self):
        self.assertIsNone(parse_voice_approval("Ayer cerré la pantalla de aportes."))


class ApproverTests(unittest.TestCase):
    def test_match_ignores_case_and_accents(self):
        self.assertTrue(is_voice_approver("DIEGO ALCIDES CARABALI", ["Diego Alcides Carabali"]))
        self.assertTrue(is_voice_approver("Diego Alcídes Carabalí", ["Diego Alcides Carabali"]))

    def test_other_people_not_approvers(self):
        self.assertFalse(is_voice_approver("Jorge Eduardo Tascon Cordoba", ["Diego Alcides Carabali"]))
        self.assertFalse(is_voice_approver("", ["Diego Alcides Carabali"]))


class VoiceFlowTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.ran = []
        self._orig_send = copilot.recall.send_chat
        self._orig_spawn = copilot._spawn
        self._orig_approvers = copilot.config.VOICE_APPROVERS

        async def fake_send(bot_id, text, **kw):
            self.sent.append(text)

        copilot.recall.send_chat = fake_send
        copilot._spawn = lambda coro: (self.ran.append(coro), coro.close())
        copilot.config.VOICE_APPROVERS = ["Diego Alcides Carabali"]

        self.s = MeetingSession(bot_id="b1", agenda="daily")
        self.s.new_proposal(summary="Crear reunión", instructions="x", server="calendar", evidence="y")

    def tearDown(self):
        copilot.recall.send_chat = self._orig_send
        copilot._spawn = self._orig_spawn
        copilot.config.VOICE_APPROVERS = self._orig_approvers

    def test_approver_runs_pending_proposal_once(self):
        asyncio.run(copilot._maybe_voice_approve(self.s, "Diego Alcides Carabali", "aprueba la propuesta uno"))
        asyncio.run(copilot._maybe_voice_approve(self.s, "Diego Alcides Carabali", "aprueba la propuesta uno"))
        self.assertEqual(len(self.ran), 1)
        self.assertEqual(self.s.proposals[1].status, "approved")
        self.assertIn("aprobada por voz de Diego Alcides Carabali", self.sent[0])

    def test_other_speaker_cannot_approve(self):
        asyncio.run(copilot._maybe_voice_approve(self.s, "Jorge Eduardo Tascon Cordoba", "ok 1"))
        self.assertEqual(self.ran, [])
        self.assertEqual(self.s.proposals[1].status, "pending")

    def test_unknown_proposal_ignored(self):
        asyncio.run(copilot._maybe_voice_approve(self.s, "Diego Alcides Carabali", "ok 9"))
        self.assertEqual(self.ran, [])


if __name__ == "__main__":
    unittest.main()
