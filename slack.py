"""Slack integration for Eddie notifications."""

import hashlib
import hmac
import logging
import time

import httpx

import config

logger = logging.getLogger(__name__)


def send_slack_message(text: str) -> None:
    """Post a message to the Eddie dispatch Slack channel via Incoming Webhook."""
    if not config.SLACK_WEBHOOK_URL:
        logger.warning("SLACK_WEBHOOK_URL not set, skipping Slack message")
        return

    resp = httpx.post(config.SLACK_WEBHOOK_URL, json={"text": text}, timeout=10)
    resp.raise_for_status()
    logger.info("Slack message sent: %s", text[:80])


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
