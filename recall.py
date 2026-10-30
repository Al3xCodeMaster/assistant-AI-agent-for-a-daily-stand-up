"""Thin Recall.ai client: create bot, send chat, verify signed requests."""
import asyncio
import base64
import hashlib
import hmac
import logging

import config

log = logging.getLogger("recall")


def _base() -> str:
    return f"https://{config.RECALL_REGION}.recall.ai/api/v1"


def _headers() -> dict:
    return {
        "Authorization": config.RECALL_API_KEY,
        "accept": "application/json",
        "content-type": "application/json",
    }


def build_bot_payload(meeting_url: str, agenda: str) -> dict:
    return {
        "meeting_url": meeting_url,
        "bot_name": config.BOT_NAME,
        "recording_config": {
            "transcript": {
                "provider": {
                    "recallai_streaming": {
                        # The default mode (prioritize_accuracy) delays utterances by minutes.
                        "mode": "prioritize_low_latency",
                        "language_code": config.TRANSCRIPT_LANGUAGE,
                    }
                },
                "diarization": {"use_separate_streams_when_available": True},
            },
            "realtime_endpoints": [
                {
                    "type": "webhook",
                    "url": f"{config.PUBLIC_URL}/webhooks/recall",
                    "events": ["transcript.data", "participant_events.chat_message"],
                }
            ],
        },
        "chat": {
            "on_bot_join": {
                "send_to": "everyone",
                "pin": True,
                "message": (
                    f"{config.CHAT_PREFIX} Hi! I'm transcribing this meeting to track action items "
                    "and keep us on the agenda. Reply 'ok <n>' / 'no <n>' to my proposals."
                )[:500],
            }
        },
        "metadata": {"agenda": agenda[:500]},
    }


async def create_bot(meeting_url: str, agenda: str) -> dict:
    """Create a bot; retries 507 (capacity) every 30s up to 10 times, as Recall recommends."""
    import httpx

    payload = build_bot_payload(meeting_url, agenda)
    async with httpx.AsyncClient(timeout=30) as http:
        for attempt in range(10):
            r = await http.post(f"{_base()}/bot/", headers=_headers(), json=payload)
            if r.status_code != 507:
                r.raise_for_status()
                return r.json()
            log.warning("Recall returned 507, retrying in 30s (attempt %d)", attempt + 1)
            await asyncio.sleep(30)
    raise RuntimeError("Recall kept returning 507; schedule the bot further in advance.")


async def send_chat(bot_id: str, message: str, pin: bool = False) -> None:
    message = f"{config.CHAT_PREFIX} {message}"[:500]  # Google Meet limit is 500 chars
    if config.DRY_RUN:
        print(f"[chat → {bot_id}] {message}")
        return
    import httpx

    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.post(
            f"{_base()}/bot/{bot_id}/send_chat_message/",
            headers=_headers(),
            json={"to": "everyone", "message": message, "pin": pin},
        )
        if r.status_code >= 300:
            log.error("send_chat failed %s: %s", r.status_code, r.text[:300])


async def leave_call(bot_id: str) -> None:
    if config.DRY_RUN:
        return
    import httpx

    async with httpx.AsyncClient(timeout=15) as http:
        await http.post(f"{_base()}/bot/{bot_id}/leave_call/", headers=_headers())


def verify_request(secret: str, headers: dict, payload: bytes | None) -> None:
    """Port of Recall's verifyRequestFromRecall. Raises ValueError if invalid."""
    h = {k.lower(): v for k, v in headers.items()}
    msg_id = h.get("webhook-id") or h.get("svix-id")
    ts = h.get("webhook-timestamp") or h.get("svix-timestamp")
    sigs = h.get("webhook-signature") or h.get("svix-signature")
    if not secret or not secret.startswith("whsec_"):
        raise ValueError("Verification secret missing or invalid")
    if not (msg_id and ts and sigs):
        raise ValueError("Missing webhook id, timestamp or signature headers")

    key = base64.b64decode(secret[len("whsec_"):])
    body = (payload or b"").decode("utf-8")
    expected = hmac.new(key, f"{msg_id}.{ts}.{body}".encode(), hashlib.sha256).digest()

    for versioned in sigs.split(" "):
        version, _, sig = versioned.partition(",")
        if version != "v1":
            continue
        try:
            if hmac.compare_digest(expected, base64.b64decode(sig)):
                return
        except ValueError:
            continue
    raise ValueError("No matching signature found")


def sign_request(secret: str, msg_id: str, ts: str, payload: bytes) -> str:
    """Produce a Recall-style signature header (used by the local simulator/tests)."""
    key = base64.b64decode(secret[len("whsec_"):])
    sig = hmac.new(key, f"{msg_id}.{ts}.{payload.decode()}".encode(), hashlib.sha256).digest()
    return "v1," + base64.b64encode(sig).decode()
