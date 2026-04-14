"""SMS and signature-validation utilities for Eddie's HVAC Contractor Dispatch Agent."""

import hashlib
import hmac
import logging

from twilio.rest import Client
from twilio.request_validator import RequestValidator

import config

logger = logging.getLogger(__name__)

# Lazy-initialised Twilio client (created on first use so tests can patch config)
_client: Client | None = None


def _get_client() -> Client:
    """Return (and cache) a Twilio REST client."""
    global _client
    if _client is None:
        _client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
    return _client


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def send_sms(to: str, body: str) -> str:
    """Send an SMS via Twilio.

    Returns the Twilio message SID on success.
    Raises on failure (caller decides how to handle).
    In DRY_RUN mode, logs the message and returns a fake SID.
    """
    if config.DRY_RUN:
        import time
        fake_sid = f"DRYRUN_{int(time.time() * 1000)}"
        logger.info("[DRY RUN] SMS to %s: %s", to, body)
        return fake_sid

    client = _get_client()
    message = client.messages.create(
        to=to,
        from_=config.TWILIO_NUMBER,
        body=body,
    )
    logger.info("SMS sent to %s  sid=%s", to, message.sid)
    return message.sid


def send_error_alert(message: str) -> None:
    """Send an alert to Slack (if enabled) or SMS to the builder phone.

    Catches its own exceptions so an alert failure never crashes the app.
    """
    body = f"[HVAC DISPATCH ALERT] {message}"
    try:
        if config.SLACK_ENABLED:
            import slack as slack_module
            slack_module.send_slack_message(body)
        elif config.BUILDER_PHONE:
            send_sms(config.BUILDER_PHONE, body)
        else:
            logger.warning("No alert destination configured (no Slack, no BUILDER_PHONE)")
    except Exception:
        logger.exception("Failed to send error alert")


def send_eddie_notification(message: str) -> str:
    """Send a notification SMS to Eddie's phone.

    Returns the Twilio message SID on success.
    Raises on failure (same contract as send_sms).
    """
    return send_sms(config.EDDIE_PHONE, message)


# ---------------------------------------------------------------------------
# Signature validation
# ---------------------------------------------------------------------------

def validate_twilio_signature(request_url: str, params: dict, signature: str) -> bool:
    """Verify an ``x-twilio-signature`` header using the Twilio auth token."""
    validator = RequestValidator(config.TWILIO_AUTH_TOKEN)
    return validator.validate(request_url, params, signature)


def validate_retell_signature(payload_bytes: bytes, signature: str, api_key: str) -> bool:
    """Validate the ``x-retell-signature`` header from Retell webhooks.

    Retell uses HMAC-SHA256 with the API key as the secret, applied to the raw
    request body.
    """
    expected = hmac.new(
        api_key.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
