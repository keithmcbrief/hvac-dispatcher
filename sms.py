"""SMS and signature-validation utilities for Eddie's HVAC Contractor Dispatch Agent."""

import hashlib
import hmac
import logging
import time

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

    Retell's current signature format is ``v={timestamp_ms},d={digest}``,
    where digest is HMAC-SHA256(api_key, raw_body + timestamp_ms). The
    timestamp check limits replayed webhook attempts. A legacy plain hex digest
    is still accepted for local tests and older fixtures.
    """
    valid, _reason = validate_retell_signature_with_reason(payload_bytes, signature, api_key)
    return valid


def validate_retell_signature_with_reason(payload_bytes: bytes, signature: str, api_key: str) -> tuple[bool, dict]:
    """Validate a Retell signature and return metadata safe to log."""
    info = {
        "payload_len": len(payload_bytes),
        "signature_present": bool(signature),
        "signature_len": len(signature or ""),
        "api_key_present": bool(api_key),
        "api_key_len": len(api_key or ""),
    }

    if not api_key:
        info["reason"] = "missing_api_key"
        return False, info
    if not signature:
        info["reason"] = "missing_signature"
        return False, info

    parts = {}
    for item in signature.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        parts[key.strip()] = value.strip()

    timestamp = parts.get("v")
    digest = parts.get("d")
    info["has_timestamp"] = bool(timestamp)
    info["has_digest"] = bool(digest)
    info["timestamp_len"] = len(timestamp or "")
    info["digest_len"] = len(digest or "")

    if timestamp and digest:
        try:
            signed_at = int(timestamp)
        except ValueError:
            info["reason"] = "invalid_timestamp"
            return False, info

        delta_ms = int(time.time() * 1000) - signed_at
        info["timestamp_delta_ms"] = delta_ms
        if abs(delta_ms) > 5 * 60 * 1000:
            info["reason"] = "expired_timestamp"
            return False, info

        body_text = payload_bytes.decode("utf-8")
        expected = hmac.new(
            api_key.encode("utf-8"),
            f"{body_text}{timestamp}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        valid = hmac.compare_digest(expected, digest)
        info["matched_scheme"] = "body_timestamp" if valid else ""

        if not valid:
            timestamp_first_expected = hmac.new(
                api_key.encode("utf-8"),
                f"{timestamp}{body_text}".encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            valid = hmac.compare_digest(timestamp_first_expected, digest)
            info["matched_scheme"] = "timestamp_body" if valid else ""

        info["reason"] = "ok" if valid else "digest_mismatch"
        return valid, info

    legacy_expected = hmac.new(
        api_key.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    valid = hmac.compare_digest(legacy_expected, signature)
    info["reason"] = "legacy_ok" if valid else "malformed_or_legacy_digest_mismatch"
    return valid, info
