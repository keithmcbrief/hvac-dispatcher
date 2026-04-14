"""Eval suite — runs against real GPT-4o-mini.

Usage:
    pytest tests/test_classifier_eval.py -m eval

Requires a valid OPENAI_API_KEY in the environment.
"""

import pytest

from classifier import classify_reply

pytestmark = pytest.mark.eval


# ── Accepted ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text",
    [
        "yeah tmrw around 2ish",
        "can be there wednesday morning",
        "I'll take it, be there by 10am",
        "sure thing, Monday afternoon works",
        "yep heading over now",
        "count me in, tomorrow at 3",
        "I can do it Thursday after lunch",
    ],
)
def test_accepted_with_time(text):
    result = classify_reply(text)
    assert result["intent"] == "accepted", f"Expected accepted for: {text!r}, got {result}"
    assert result["time"] is not None, f"Expected a time for: {text!r}, got {result}"


# ── Declined ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text",
    [
        "nah man i got another job",
        "sorry can't do it, swamped all week",
        "I'm booked solid, try someone else",
        "no way, too far out",
        "not gonna make it, truck's in the shop",
        "can't, dealing with a family emergency",
    ],
)
def test_declined_with_reason(text):
    result = classify_reply(text)
    assert result["intent"] == "declined", f"Expected declined for: {text!r}, got {result}"
    assert result["reason"] is not None, f"Expected a reason for: {text!r}, got {result}"


# ── Conditional ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text",
    [
        "only if its before 5pm",
        "depends on how far the drive is",
        "I could but only if you cover parts",
        "maybe, if it's a quick job",
        "ill do it if no one else takes it by noon",
    ],
)
def test_conditional(text):
    result = classify_reply(text)
    assert result["intent"] == "conditional", f"Expected conditional for: {text!r}, got {result}"
    assert result["condition"] is not None, f"Expected a condition for: {text!r}, got {result}"


# ── Unclear ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text",
    [
        "what address again?",
        "lol",
        "ill try but no promises",
        "who is this",
        "huh",
        "send me the details",
        "???",
    ],
)
def test_unclear(text):
    result = classify_reply(text)
    assert result["intent"] == "unclear", f"Expected unclear for: {text!r}, got {result}"


# ── Emoji-only (should be handled by regex, not LLM) ────────────────────

def test_thumbs_up_emoji():
    result = classify_reply("👍👍")
    assert result["intent"] == "accepted"


def test_thumbs_down_emoji():
    result = classify_reply("👎")
    assert result["intent"] == "declined"
