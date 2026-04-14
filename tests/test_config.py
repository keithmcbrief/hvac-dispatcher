"""Tests for config module."""

import importlib
import os

def test_twilio_fallback_to_legacy_names(monkeypatch):
    """Config should fall back to ACCOUNT_SID / AUTH_TOKEN when TWILIO_* not set."""
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ACCOUNT_SID", "legacy_sid")
    monkeypatch.setenv("AUTH_TOKEN", "legacy_token")
    monkeypatch.setenv("TWILIO_NUMBER", "+15551234567")

    import config
    importlib.reload(config)

    assert config.TWILIO_ACCOUNT_SID == "legacy_sid"
    assert config.TWILIO_AUTH_TOKEN == "legacy_token"


def test_twilio_prefers_twilio_prefix(monkeypatch):
    """TWILIO_ACCOUNT_SID should take precedence over ACCOUNT_SID."""
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "new_sid")
    monkeypatch.setenv("ACCOUNT_SID", "legacy_sid")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "new_token")
    monkeypatch.setenv("AUTH_TOKEN", "legacy_token")

    import config
    importlib.reload(config)

    assert config.TWILIO_ACCOUNT_SID == "new_sid"
    assert config.TWILIO_AUTH_TOKEN == "new_token"


def test_contractors_structure(monkeypatch):
    """Contractors dict should have 3 entries with correct priorities."""
    monkeypatch.setenv("JOSE_PHONE", "+15551110001")
    monkeypatch.setenv("MARIO_PHONE", "+15551110002")
    monkeypatch.setenv("RAUL_PHONE", "+15551110003")

    import config
    importlib.reload(config)

    assert config.CONTRACTORS["Jose"]["priority"] == 1
    assert config.CONTRACTORS["Mario"]["priority"] == 2
    assert config.CONTRACTORS["Raul"]["priority"] == 3
    assert config.CONTRACTORS["Jose"]["phone"] == "+15551110001"


def test_contractor_phones_reverse_lookup(monkeypatch):
    """CONTRACTOR_PHONES should map phone -> name."""
    monkeypatch.setenv("JOSE_PHONE", "+15551110001")
    monkeypatch.setenv("MARIO_PHONE", "+15551110002")
    monkeypatch.setenv("RAUL_PHONE", "+15551110003")

    import config
    importlib.reload(config)

    assert config.CONTRACTOR_PHONES["+15551110001"] == "Jose"
    assert config.CONTRACTOR_PHONES["+15551110002"] == "Mario"
    assert config.CONTRACTOR_PHONES["+15551110003"] == "Raul"


def test_timing_constants(monkeypatch):
    """Verify timing constants have expected default values."""
    monkeypatch.delenv("FOLLOW_UP_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("POLL_INTERVAL_SECONDS", raising=False)

    import config
    importlib.reload(config)

    assert config.FOLLOW_UP_INTERVAL_SECONDS == 300
    assert config.MAX_ATTEMPTS_PER_CONTRACTOR == 3
    assert config.POLL_INTERVAL_SECONDS == 30
    assert config.STALENESS_ALERT_MINUTES == 15
    assert config.JOB_TTL_HOURS == 24
    assert config.HEARTBEAT_HOURS == 12


def test_dashboard_slug_from_env(monkeypatch):
    """DASHBOARD_SLUG should use env var when set."""
    monkeypatch.setenv("DASHBOARD_SLUG", "my-slug-01")

    import config
    importlib.reload(config)

    assert config.DASHBOARD_SLUG == "my-slug-01"


def test_dashboard_slug_generated_when_missing(monkeypatch):
    """DASHBOARD_SLUG should be an 8-char hex string when not in env."""
    monkeypatch.delenv("DASHBOARD_SLUG", raising=False)

    import config
    importlib.reload(config)

    assert len(config.DASHBOARD_SLUG) == 8
    int(config.DASHBOARD_SLUG, 16)  # should not raise


def test_db_path_default(monkeypatch):
    """DB_PATH defaults to dispatch.db."""
    monkeypatch.delenv("DB_PATH", raising=False)

    import config
    importlib.reload(config)

    assert config.DB_PATH == "dispatch.db"
