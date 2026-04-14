"""Tests for sms module."""

import hashlib
import hmac
import importlib
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_sms(monkeypatch, tmp_path):
    """Isolate config and reset sms module state between tests."""
    # Write empty .env so load_dotenv doesn't leak real creds
    env_file = tmp_path / ".env"
    env_file.write_text("")
    monkeypatch.chdir(tmp_path)

    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test_auth_token")
    monkeypatch.setenv("TWILIO_NUMBER", "+15550000000")
    monkeypatch.setenv("BUILDER_PHONE", "+15551111111")
    monkeypatch.setenv("EDDIE_PHONE", "+15552222222")
    monkeypatch.setenv("RETELL_API_KEY", "retell_test_key")

    import config
    importlib.reload(config)

    import sms
    # Reset cached client so each test starts fresh
    sms._client = None
    importlib.reload(sms)

    yield sms


@pytest.fixture
def mock_client():
    """Provide a mock Twilio client and patch _get_client to return it."""
    client = MagicMock()
    msg = MagicMock()
    msg.sid = "SM_TEST_SID_123"
    client.messages.create.return_value = msg
    return client


# ---------------------------------------------------------------------------
# send_sms
# ---------------------------------------------------------------------------

class TestSendSms:
    def test_returns_sid(self, _reset_sms, mock_client):
        sms = _reset_sms
        with patch.object(sms, "_get_client", return_value=mock_client):
            sid = sms.send_sms("+15559999999", "Hello")
        assert sid == "SM_TEST_SID_123"
        mock_client.messages.create.assert_called_once_with(
            to="+15559999999",
            from_="+15550000000",
            body="Hello",
        )

    def test_raises_on_failure(self, _reset_sms, mock_client):
        sms = _reset_sms
        mock_client.messages.create.side_effect = RuntimeError("Twilio down")
        with patch.object(sms, "_get_client", return_value=mock_client):
            with pytest.raises(RuntimeError, match="Twilio down"):
                sms.send_sms("+15559999999", "Hello")


# ---------------------------------------------------------------------------
# send_error_alert
# ---------------------------------------------------------------------------

class TestSendErrorAlert:
    def test_sends_prefixed_message(self, _reset_sms, mock_client):
        sms = _reset_sms
        with patch.object(sms, "_get_client", return_value=mock_client):
            sms.send_error_alert("Job stuck")
        mock_client.messages.create.assert_called_once_with(
            to="+15551111111",
            from_="+15550000000",
            body="[HVAC DISPATCH ALERT] Job stuck",
        )

    def test_catches_own_exceptions(self, _reset_sms, mock_client):
        sms = _reset_sms
        mock_client.messages.create.side_effect = RuntimeError("boom")
        with patch.object(sms, "_get_client", return_value=mock_client):
            # Should NOT raise
            sms.send_error_alert("Job stuck")

    def test_catches_own_exceptions_no_crash(self, _reset_sms, mock_client):
        """Even a completely broken client should not crash the caller."""
        sms = _reset_sms
        with patch.object(sms, "_get_client", side_effect=RuntimeError("no client")):
            sms.send_error_alert("test")


# ---------------------------------------------------------------------------
# send_eddie_notification
# ---------------------------------------------------------------------------

class TestSendEddieNotification:
    def test_sends_to_eddie_phone(self, _reset_sms, mock_client):
        sms = _reset_sms
        with patch.object(sms, "_get_client", return_value=mock_client):
            sid = sms.send_eddie_notification("New job created")
        assert sid == "SM_TEST_SID_123"
        mock_client.messages.create.assert_called_once_with(
            to="+15552222222",
            from_="+15550000000",
            body="New job created",
        )

    def test_raises_on_failure(self, _reset_sms, mock_client):
        sms = _reset_sms
        mock_client.messages.create.side_effect = RuntimeError("fail")
        with patch.object(sms, "_get_client", return_value=mock_client):
            with pytest.raises(RuntimeError):
                sms.send_eddie_notification("msg")


# ---------------------------------------------------------------------------
# validate_twilio_signature
# ---------------------------------------------------------------------------

class TestValidateTwilioSignature:
    def test_valid_signature(self, _reset_sms):
        sms = _reset_sms
        from twilio.request_validator import RequestValidator

        # Build a known-good signature using the same token
        validator = RequestValidator("test_auth_token")
        url = "https://example.com/webhook"
        params = {"Body": "hello", "From": "+15559999999"}
        good_sig = validator.compute_signature(url, params)

        assert sms.validate_twilio_signature(url, params, good_sig) is True

    def test_invalid_signature(self, _reset_sms):
        sms = _reset_sms
        url = "https://example.com/webhook"
        params = {"Body": "hello"}
        assert sms.validate_twilio_signature(url, params, "bad_signature") is False


# ---------------------------------------------------------------------------
# validate_retell_signature
# ---------------------------------------------------------------------------

class TestValidateRetellSignature:
    def test_valid_signature(self, _reset_sms, monkeypatch):
        sms = _reset_sms
        api_key = "my_secret_key"
        payload = b'{"event":"call_ended"}'
        monkeypatch.setattr(sms.time, "time", lambda: 1_700_000_000)
        timestamp = "1700000000000"
        digest = hmac.new(
            api_key.encode(),
            payload + timestamp.encode(),
            hashlib.sha256,
        ).hexdigest()
        sig = f"v={timestamp},d={digest}"

        assert sms.validate_retell_signature(payload, sig, api_key) is True

    def test_invalid_signature(self, _reset_sms):
        sms = _reset_sms
        payload = b'{"event":"call_ended"}'
        assert sms.validate_retell_signature(payload, "wrong", "key") is False

    def test_tampered_payload(self, _reset_sms, monkeypatch):
        sms = _reset_sms
        api_key = "secret"
        original = b'{"amount":100}'
        monkeypatch.setattr(sms.time, "time", lambda: 1_700_000_000)
        timestamp = "1700000000000"
        digest = hmac.new(
            api_key.encode(),
            original + timestamp.encode(),
            hashlib.sha256,
        ).hexdigest()
        sig = f"v={timestamp},d={digest}"

        tampered = b'{"amount":999}'
        assert sms.validate_retell_signature(tampered, sig, api_key) is False

    def test_expired_signature(self, _reset_sms, monkeypatch):
        sms = _reset_sms
        api_key = "secret"
        payload = b'{"event":"call_ended"}'
        monkeypatch.setattr(sms.time, "time", lambda: 1_700_000_600)
        timestamp = "1700000000000"
        digest = hmac.new(
            api_key.encode(),
            payload + timestamp.encode(),
            hashlib.sha256,
        ).hexdigest()

        assert sms.validate_retell_signature(payload, f"v={timestamp},d={digest}", api_key) is False

    def test_legacy_plain_hex_signature(self, _reset_sms):
        sms = _reset_sms
        api_key = "my_secret_key"
        payload = b'{"event":"call_ended"}'
        sig = hmac.new(api_key.encode(), payload, hashlib.sha256).hexdigest()

        assert sms.validate_retell_signature(payload, sig, api_key) is True

    def test_compact_json_signature(self, _reset_sms, monkeypatch):
        sms = _reset_sms
        api_key = "secret"
        payload = b'{\n  "event": "call_ended",\n  "call": {"id": "abc"}\n}'
        compact_payload = b'{"event":"call_ended","call":{"id":"abc"}}'
        monkeypatch.setattr(sms.time, "time", lambda: 1_700_000_000)
        timestamp = "1700000000000"
        digest = hmac.new(
            api_key.encode(),
            compact_payload + timestamp.encode(),
            hashlib.sha256,
        ).hexdigest()

        assert sms.validate_retell_signature(payload, f"v={timestamp},d={digest}", api_key) is True

    def test_signature_without_key_prefix(self, _reset_sms, monkeypatch):
        sms = _reset_sms
        api_key = "key_abc123"
        payload = b'{"event":"call_ended"}'
        monkeypatch.setattr(sms.time, "time", lambda: 1_700_000_000)
        timestamp = "1700000000000"
        digest = hmac.new(
            b"abc123",
            payload + timestamp.encode(),
            hashlib.sha256,
        ).hexdigest()

        assert sms.validate_retell_signature(payload, f"v={timestamp},d={digest}", api_key) is True
