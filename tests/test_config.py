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
    """Contractors dict should keep Jose paused and preserve original priority order."""
    monkeypatch.setenv("JOSE_PHONE", "+15551110001")
    monkeypatch.setenv("MARIO_PHONE", "+15551110002")
    monkeypatch.setenv("RAUL_PHONE", "+15551110003")
    monkeypatch.delenv("JOSE_ACTIVE", raising=False)
    monkeypatch.delenv("MARIO_ACTIVE", raising=False)
    monkeypatch.delenv("RAUL_ACTIVE", raising=False)

    import config
    importlib.reload(config)

    assert config.CONTRACTORS["Jose"]["priority"] == 1
    assert config.CONTRACTORS["Mario"]["priority"] == 2
    assert config.CONTRACTORS["Raul"]["priority"] == 3
    assert config.CONTRACTORS["Jose"]["phone"] == "+15551110001"
    assert config.CONTRACTORS["Jose"]["active"] is False
    assert config.CONTRACTORS["Mario"]["active"] is True
    assert config.CONTRACTORS["Raul"]["active"] is True


def test_contractor_phones_reverse_lookup(monkeypatch):
    """CONTRACTOR_PHONES should map phone -> name."""
    monkeypatch.setenv("JOSE_PHONE", "+15551110001")
    monkeypatch.setenv("MARIO_PHONE", "+15551110002")
    monkeypatch.setenv("RAUL_PHONE", "+15551110003")
    monkeypatch.delenv("JOSE_ACTIVE", raising=False)

    import config
    importlib.reload(config)

    # Jose stays recognizable for replies to any already-assigned jobs while paused.
    assert config.CONTRACTOR_PHONES["+15551110001"] == "Jose"
    assert config.CONTRACTOR_PHONES["+15551110002"] == "Mario"
    assert config.CONTRACTOR_PHONES["+15551110003"] == "Raul"


def test_timing_constants(monkeypatch):
    """Verify timing constants have expected default values."""
    monkeypatch.delenv("FOLLOW_UP_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("POLL_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("JOB_POLLING_ENABLED", raising=False)
    monkeypatch.delenv("CUSTOMER_CONFIRMATION_SMS_ENABLED", raising=False)
    monkeypatch.delenv("HEARTBEAT_HOURS", raising=False)
    monkeypatch.delenv("HEARTBEAT_ALERT_INTERVAL_HOURS", raising=False)

    import config
    importlib.reload(config)

    assert config.FOLLOW_UP_INTERVAL_SECONDS == 300
    assert config.MAX_ATTEMPTS_PER_CONTRACTOR == 3
    assert config.POLL_INTERVAL_SECONDS == 30
    assert config.STALENESS_ALERT_MINUTES == 15
    assert config.JOB_TTL_HOURS == 24
    assert config.JOB_POLLING_ENABLED is False
    assert config.CUSTOMER_CONFIRMATION_SMS_ENABLED is False
    assert config.HEARTBEAT_HOURS == 12
    assert config.HEARTBEAT_ALERT_INTERVAL_HOURS == 24


def test_automation_flags_can_be_enabled(monkeypatch):
    """Polling and customer confirmation can be restored by env flag."""
    monkeypatch.setenv("JOB_POLLING_ENABLED", "true")
    monkeypatch.setenv("CUSTOMER_CONFIRMATION_SMS_ENABLED", "true")

    import config
    importlib.reload(config)

    assert config.JOB_POLLING_ENABLED is True
    assert config.CUSTOMER_CONFIRMATION_SMS_ENABLED is True


def test_system_alerts_enabled_by_default(monkeypatch):
    """System/error alerts should be enabled unless explicitly muted."""
    monkeypatch.delenv("SYSTEM_ALERTS_ENABLED", raising=False)

    import config
    importlib.reload(config)

    assert config.SYSTEM_ALERTS_ENABLED is True


def test_system_alerts_can_be_disabled(monkeypatch):
    """SYSTEM_ALERTS_ENABLED=false is the emergency alert mute switch."""
    monkeypatch.setenv("SYSTEM_ALERTS_ENABLED", "false")

    import config
    importlib.reload(config)

    assert config.SYSTEM_ALERTS_ENABLED is False


def test_discord_notification_provider_from_env(monkeypatch):
    """Discord is selected automatically when its webhook URL is configured."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "true")
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("NOTIFICATION_PROVIDER", raising=False)

    import config
    importlib.reload(config)

    assert config.NOTIFICATION_PROVIDER == "discord"
    assert config.NOTIFICATIONS_ENABLED is True
    assert config.SLACK_ENABLED is False


def test_both_notification_provider_when_both_urls_present(monkeypatch):
    """Both chat destinations are selected automatically when both URLs exist."""
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://slack.example/webhook")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "true")
    monkeypatch.delenv("NOTIFICATION_PROVIDER", raising=False)

    import config
    importlib.reload(config)

    assert config.NOTIFICATION_PROVIDER == "both"
    assert config.NOTIFICATIONS_ENABLED is True
    assert config.SLACK_ENABLED is True


def test_legacy_slack_enabled_still_works(monkeypatch):
    """SLACK_ENABLED remains supported for existing deployments."""
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("NOTIFICATION_PROVIDER", raising=False)
    monkeypatch.delenv("NOTIFICATIONS_ENABLED", raising=False)
    monkeypatch.setenv("SLACK_ENABLED", "true")

    import config
    importlib.reload(config)

    assert config.NOTIFICATION_PROVIDER == "slack"
    assert config.NOTIFICATIONS_ENABLED is True
    assert config.SLACK_ENABLED is True


def test_business_name_default(monkeypatch):
    """Customer-facing SMS copy should have a business name fallback."""
    monkeypatch.delenv("BUSINESS_NAME", raising=False)

    import config
    importlib.reload(config)

    assert config.BUSINESS_NAME == "Residential AC & Heating"


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
