"""Tests for Slack formatting helpers."""

import slack


def test_format_transcript_for_slack_empty():
    assert slack.format_transcript_for_slack("") == ""


def test_format_transcript_for_slack_wraps_code_block():
    block = slack.format_transcript_for_slack("Agent: hi\nUser: hello")

    assert "Full transcript:" in block
    assert "```Agent: hi\nUser: hello```" in block


def test_format_transcript_for_slack_sanitizes_backticks():
    block = slack.format_transcript_for_slack("Agent: ```")

    assert "```Agent: '''```" in block


def test_format_transcript_for_slack_truncates_long_text():
    block = slack.format_transcript_for_slack("a" * 10, max_chars=5)

    assert "aaaaa" in block
    assert "Transcript truncated in Slack" in block
