"""Replay a scripted meeting against a local server, signed like Recall would sign it.

Usage (server running with DRY_RUN=true):
    python tools/simulate.py [--url http://localhost:8000] [--speed 3]
Bot chat messages are printed by the server instead of sent to Meet.
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config  # noqa: E402
from recall import sign_request  # noqa: E402

BOT_ID = "sim-bot"
AGENDA = "1) Q4 mobile release date 2) Pricing change for Pro plan 3) Hiring plan for support"

SCRIPT = [
    ("Ana", "Okay let's start with the mobile release. Where are we on the iOS build?"),
    ("Luis", "QA found a crash on login with Apple ID, so the build is blocked."),
    ("Ana", "Luis, can you open a ticket for that login crash and own it? Due Friday."),
    ("Luis", "Sure, I'll take it."),
    ("Ana", "Then the release moves to November 12th. Let's schedule a go/no-go meeting on November 10th at 10am with the whole team."),
    ("Carla", "By the way, did anyone watch the match last night? Unbelievable ending."),
    ("Luis", "Yes! That penalty in the 90th minute, I couldn't believe the referee."),
    ("Carla", "And the coach's reaction afterwards, he was furious, the press conference was wild."),
    ("Luis", "I think they'll fire him before December honestly, the fans are really angry."),
    ("Carla", "My brother went to the stadium, he said the atmosphere was incredible even after losing."),
    ("Luis", "We should all go to a game together sometime, maybe in December."),
]


def post(url: str, body: dict, signed: bool = True) -> None:
    raw = json.dumps(body).encode()
    headers = {"content-type": "application/json"}
    if signed:
        msg_id, ts = f"msg_{uuid.uuid4().hex[:16]}", str(int(time.time()))
        headers |= {
            "webhook-id": msg_id,
            "webhook-timestamp": ts,
            "webhook-signature": sign_request(config.RECALL_VERIFICATION_SECRET, msg_id, ts, raw),
        }
    req = urllib.request.Request(url, data=raw, headers=headers, method="POST")
    with urllib.request.urlopen(req) as r:
        r.read()


def transcript_event(speaker: str, text: str, pid: int) -> dict:
    return {
        "event": "transcript.data",
        "data": {
            "data": {
                "words": [{"text": w, "start_timestamp": {"relative": 0.0}, "end_timestamp": None} for w in text.split()],
                "language_code": "en",
                "participant": {"id": pid, "name": speaker, "is_host": speaker == "Ana"},
            },
            "bot": {"id": BOT_ID, "metadata": {}},
        },
    }


def chat_event(sender: str, text: str) -> dict:
    return {
        "event": "participant_events.chat_message",
        "data": {
            "data": {"participant": {"id": 1, "name": sender}, "data": {"text": text}},
            "bot": {"id": BOT_ID, "metadata": {}},
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--speed", type=float, default=3.0, help="seconds between utterances")
    args = ap.parse_args()

    post(f"{args.url}/meetings", {"agenda": AGENDA, "bot_id": BOT_ID}, signed=False)
    ids = {}
    for speaker, text in SCRIPT:
        ids.setdefault(speaker, len(ids) + 100)
        print(f"{speaker}: {text}")
        post(f"{args.url}/webhooks/recall", transcript_event(speaker, text, ids[speaker]))
        time.sleep(args.speed)

    print("\nWaiting for proposals and focus checks... then approving #1")
    time.sleep(20)
    post(f"{args.url}/webhooks/recall", chat_event("Ana", "ok 1"))
    time.sleep(15)
    post(f"{args.url}/webhooks/recall", chat_event("Ana", "stats"))
    time.sleep(2)
    with urllib.request.urlopen(f"{args.url}/meetings/{BOT_ID}") as r:
        print(json.dumps(json.load(r), indent=2))
    with urllib.request.urlopen(f"{args.url}/meetings/{BOT_ID}/metrics") as r:
        print(json.dumps(json.load(r), indent=2))


if __name__ == "__main__":
    main()
