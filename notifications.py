"""Outbound notification integrations for Eddie notifications."""

import logging

import httpx

import config

logger = logging.getLogger(__name__)

MAX_TRANSCRIPT_CHARS = 30_000
DISCORD_CONTENT_LIMIT = 1900


def format_transcript(transcript: str, max_chars: int = MAX_TRANSCRIPT_CHARS) -> str:
    """Return a code-block transcript safe for chat webhook notifications."""
    transcript = (transcript or "").strip()
    if not transcript:
        return ""

    transcript = transcript.replace("```", "'''")
    if len(transcript) > max_chars:
        transcript = transcript[:max_chars].rstrip()
        transcript = f"{transcript}\n[Transcript truncated in notification.]"

    return f"\n\nFull transcript:\n```{transcript}```"


def _destinations() -> list[tuple[str, str]]:
    provider = (config.NOTIFICATION_PROVIDER or "").lower()
    if provider == "discord":
        return [("discord", config.DISCORD_WEBHOOK_URL)]
    if provider == "slack":
        return [("slack", config.SLACK_WEBHOOK_URL)]
    if provider == "both":
        return [
            ("slack", config.SLACK_WEBHOOK_URL),
            ("discord", config.DISCORD_WEBHOOK_URL),
        ]
    if provider == "generic":
        return [("generic", config.NOTIFICATION_WEBHOOK_URL)]
    return [(provider or "slack", config.SLACK_WEBHOOK_URL)]


def _discord_chunks(text: str) -> list[str]:
    """Split Discord webhook content into messages below Discord's hard limit."""
    if len(text) <= DISCORD_CONTENT_LIMIT:
        return [text]

    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= DISCORD_CONTENT_LIMIT:
            chunks.append(remaining)
            break

        split_at = remaining.rfind("\n", 0, DISCORD_CONTENT_LIMIT)
        if split_at < DISCORD_CONTENT_LIMIT // 2:
            split_at = DISCORD_CONTENT_LIMIT

        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()

    return chunks


def send_message(text: str) -> None:
    """Post a message to the configured chat webhook destination(s)."""
    if not config.NOTIFICATIONS_ENABLED:
        logger.info("Notifications disabled, skipping message")
        return

    attempted = 0
    sent = 0
    errors = []

    for provider, url in _destinations():
        if not url:
            logger.warning("%s webhook URL not set, skipping notification", provider)
            continue

        attempted += 1
        try:
            if provider == "discord":
                for chunk in _discord_chunks(text):
                    resp = httpx.post(url, json={"content": chunk}, timeout=10)
                    resp.raise_for_status()
            else:
                resp = httpx.post(url, json={"text": text}, timeout=10)
                resp.raise_for_status()
        except Exception as exc:
            errors.append(exc)
            logger.exception("Failed to send %s notification", provider)
            continue

        sent += 1
        logger.info("%s notification sent: %s", provider, text[:80])

    if attempted and not sent and errors:
        raise errors[0]
