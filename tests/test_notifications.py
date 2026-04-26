"""Tests for outbound notification helpers."""

import importlib
from unittest.mock import patch

import httpx
import pytest


def test_send_message_posts_discord_content_payload(monkeypatch):
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("NOTIFICATION_PROVIDER", "discord")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/webhook")

    import config
    import notifications

    importlib.reload(config)
    importlib.reload(notifications)

    with patch("notifications.httpx.post") as mock_post:
        mock_post.return_value.raise_for_status.return_value = None
        notifications.send_message("Job ready")

    mock_post.assert_called_once_with(
        "https://discord.example/webhook",
        json={"content": "Job ready"},
        timeout=10,
    )


def test_send_message_splits_long_discord_messages(monkeypatch):
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("NOTIFICATION_PROVIDER", "discord")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/webhook")

    import config
    import notifications

    importlib.reload(config)
    importlib.reload(notifications)

    with patch("notifications.httpx.post") as mock_post:
        mock_post.return_value.raise_for_status.return_value = None
        notifications.send_message("a" * 2500)

    assert mock_post.call_count == 2
    assert all(
        len(call.kwargs["json"]["content"]) <= notifications.DISCORD_CONTENT_LIMIT
        for call in mock_post.call_args_list
    )


def test_send_message_posts_generic_text_payload(monkeypatch):
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("NOTIFICATION_PROVIDER", "generic")
    monkeypatch.setenv("NOTIFICATION_WEBHOOK_URL", "https://example.test/webhook")

    import config
    import notifications

    importlib.reload(config)
    importlib.reload(notifications)

    with patch("notifications.httpx.post") as mock_post:
        mock_post.return_value.raise_for_status.return_value = None
        notifications.send_message("Job ready")

    mock_post.assert_called_once_with(
        "https://example.test/webhook",
        json={"text": "Job ready"},
        timeout=10,
    )


def test_send_message_posts_to_slack_and_discord(monkeypatch):
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("NOTIFICATION_PROVIDER", "both")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://slack.example/webhook")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/webhook")

    import config
    import notifications

    importlib.reload(config)
    importlib.reload(notifications)

    with patch("notifications.httpx.post") as mock_post:
        mock_post.return_value.raise_for_status.return_value = None
        notifications.send_message("Job ready")

    assert mock_post.call_count == 2
    mock_post.assert_any_call(
        "https://slack.example/webhook",
        json={"text": "Job ready"},
        timeout=10,
    )
    mock_post.assert_any_call(
        "https://discord.example/webhook",
        json={"content": "Job ready"},
        timeout=10,
    )


def test_send_message_still_posts_discord_when_slack_fails(monkeypatch):
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("NOTIFICATION_PROVIDER", "both")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://slack.example/webhook")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/webhook")

    import config
    import notifications

    importlib.reload(config)
    importlib.reload(notifications)

    def fake_post(url, **kwargs):
        if "slack" in url:
            raise httpx.ConnectError("slack unavailable")
        response = httpx.Response(204, request=httpx.Request("POST", url))
        response.raise_for_status()
        return response

    with patch("notifications.httpx.post", side_effect=fake_post) as mock_post:
        notifications.send_message("Job ready")

    assert mock_post.call_count == 2
    assert [call.args[0] for call in mock_post.call_args_list] == [
        "https://slack.example/webhook",
        "https://discord.example/webhook",
    ]


def test_send_message_raises_when_all_configured_destinations_fail(monkeypatch):
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("NOTIFICATION_PROVIDER", "both")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://slack.example/webhook")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/webhook")

    import config
    import notifications

    importlib.reload(config)
    importlib.reload(notifications)

    with patch(
        "notifications.httpx.post",
        side_effect=httpx.ConnectError("webhook unavailable"),
    ) as mock_post:
        with pytest.raises(httpx.ConnectError):
            notifications.send_message("Job ready")

    assert mock_post.call_count == 2
