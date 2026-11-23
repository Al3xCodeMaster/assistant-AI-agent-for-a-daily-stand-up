"""Notice to the team before the bot is admitted: a Slack or Google Chat incoming webhook.

Both accept a JSON body with a "text" field. The webhook posts to one channel or space; a direct
message to one person needs the Slack MCP server instead.
"""
import logging

import config

log = logging.getLogger("notify")


def build_message(title: str, meeting_url: str) -> str:
    return (f"Reunión: {title or 'Daily'}\n"
            f"Únete aquí: {meeting_url}\n"
            "El copiloto entra a la reunión y esperará a que lo admitan desde la sala de espera.")


async def send_meeting_notice(title: str, meeting_url: str) -> bool:
    """Returns False when no webhook is configured or the post fails. Never raises: the meeting still starts."""
    if not config.NOTIFY_WEBHOOK_URL:
        return False
    import httpx

    try:
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.post(config.NOTIFY_WEBHOOK_URL, json={"text": build_message(title, meeting_url)})
            if r.status_code >= 300:
                log.error("notify failed %s: %s", r.status_code, r.text[:200])
                return False
            return True
    except Exception:  # noqa: BLE001
        log.exception("notify failed")
        return False
