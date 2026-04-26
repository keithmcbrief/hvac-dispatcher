"""Slack request validation and legacy notification aliases."""

import hashlib
import hmac
import time

import config
import notifications


MAX_TRANSCRIPT_CHARS = notifications.MAX_TRANSCRIPT_CHARS


def format_transcript_for_slack(transcript: str, max_chars: int = MAX_TRANSCRIPT_CHARS) -> str:
    """Return a chat-safe transcript block for appending to notifications."""
    return notifications.format_transcript(transcript, max_chars=max_chars)


def send_slack_message(text: str) -> None:
    """Legacy alias for the configured outbound notification provider."""
    notifications.send_message(text)


def validate_slack_request(timestamp: str, body: bytes, signature: str) -> bool:
    """Verify a request came from Slack using the signing secret.

    Slack signs requests with HMAC-SHA256 using:
      base_string = f"v0:{timestamp}:{body}"
      signature = "v0=" + hmac_sha256(signing_secret, base_string)
    """
    if not config.SLACK_SIGNING_SECRET:
        return False

    # Reject requests older than 5 minutes
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except (ValueError, TypeError):
        return False

    base_string = f"v0:{timestamp}:{body.decode('utf-8')}"
    expected = "v0=" + hmac.new(
        config.SLACK_SIGNING_SECRET.encode(),
        base_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature)
