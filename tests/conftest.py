"""Shared test fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """Prevent load_dotenv(override=True) from reading the real .env during tests."""
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: None)
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "false")
    monkeypatch.setenv("SLACK_ENABLED", "false")
    monkeypatch.delenv("SKIP_SIGNATURE_VALIDATION", raising=False)
