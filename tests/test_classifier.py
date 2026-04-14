"""Unit tests for classifier module — regex tier + mocked LLM tier."""

import json
from unittest.mock import MagicMock, patch

import openai
import pytest

from classifier import classify_reply


# ── Helpers ──────────────────────────────────────────────────────────────

def _mock_openai_response(payload: dict):
    """Build a fake OpenAI ChatCompletion response object."""
    message = MagicMock()
    message.content = json.dumps(payload)
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    return response


# ── Tier 1: Regex — obvious YES ─────────────────────────────────────────

@pytest.mark.parametrize(
    "text",
    [
        "yes",
        "Yes",
        "YES",
        "yeah",
        "yep",
        "yea",
        "ya",
        "ok",
        "OK",
        "okay",
        "sure",
        "Sure!",
        "sounds good",
        "Sounds Good",
        "I'm in",
        "im in",
        "on it",
        "On it!",
        "  yes  ",
        "👍",
        "✅",
        "👍👍",
        "✅✅",
    ],
)
def test_regex_accepts_obvious_yes(text):
    result = classify_reply(text)
    assert result["intent"] == "accepted"
    assert result["time"] is None
    assert result["raw_text"] == text


# ── Tier 1: Regex — obvious NO ──────────────────────────────────────────

@pytest.mark.parametrize(
    "text",
    [
        "no",
        "No",
        "NO",
        "nah",
        "can't",
        "cant",
        "cannot",
        "pass",
        "busy",
        "Busy",
        "not available",
        "Not Available",
        "unavailable",
        "  no  ",
        "👎",
        "❌",
        "👎👎",
    ],
)
def test_regex_declines_obvious_no(text):
    result = classify_reply(text)
    assert result["intent"] == "declined"
    assert result["reason"] is None
    assert result["raw_text"] == text


# ── Tier 1: should NOT match (falls through to LLM) ─────────────────────

@pytest.mark.parametrize(
    "text",
    [
        "yes I can be there at 2pm",
        "no sorry man got another job lined up",
        "only if before 5pm",
        "what address again?",
        "lol",
    ],
)
def test_regex_does_not_match_complex_texts(text):
    """Complex texts should NOT be caught by regex; they go to the LLM."""
    with patch("classifier._classify_with_llm") as mock_llm:
        mock_llm.return_value = {
            "intent": "unclear",
            "time": None,
            "reason": None,
            "condition": None,
            "raw_text": text,
        }
        result = classify_reply(text)
        mock_llm.assert_called_once_with(text)
        assert result["raw_text"] == text


# ── Tier 2: LLM — accepted with time ────────────────────────────────────

def test_llm_accepted_with_time():
    payload = {"intent": "accepted", "time": "Tuesday 2pm", "reason": None, "condition": None}
    with patch("classifier.openai.OpenAI") as MockClient:
        instance = MockClient.return_value
        instance.chat.completions.create.return_value = _mock_openai_response(payload)
        result = classify_reply("yeah I can do Tuesday around 2")
        assert result["intent"] == "accepted"
        assert result["time"] == "Tuesday 2pm"


# ── Tier 2: LLM — declined with reason ──────────────────────────────────

def test_llm_declined_with_reason():
    payload = {"intent": "declined", "time": None, "reason": "got another job", "condition": None}
    with patch("classifier.openai.OpenAI") as MockClient:
        instance = MockClient.return_value
        instance.chat.completions.create.return_value = _mock_openai_response(payload)
        result = classify_reply("nah man I got another job lined up")
        assert result["intent"] == "declined"
        assert result["reason"] == "got another job"


# ── Tier 2: LLM — conditional ───────────────────────────────────────────

def test_llm_conditional():
    payload = {"intent": "conditional", "time": None, "reason": None, "condition": "only if before 5pm"}
    with patch("classifier.openai.OpenAI") as MockClient:
        instance = MockClient.return_value
        instance.chat.completions.create.return_value = _mock_openai_response(payload)
        result = classify_reply("only if its before 5pm")
        assert result["intent"] == "conditional"
        assert result["condition"] == "only if before 5pm"


# ── Tier 2: LLM — unclear ───────────────────────────────────────────────

def test_llm_unclear():
    payload = {"intent": "unclear", "time": None, "reason": None, "condition": None}
    with patch("classifier.openai.OpenAI") as MockClient:
        instance = MockClient.return_value
        instance.chat.completions.create.return_value = _mock_openai_response(payload)
        result = classify_reply("what address again?")
        assert result["intent"] == "unclear"


# ── Error handling: OpenAI down → unclear ────────────────────────────────

def test_openai_error_returns_unclear():
    with patch("classifier.openai.OpenAI") as MockClient:
        instance = MockClient.return_value
        instance.chat.completions.create.side_effect = openai.APIConnectionError(request=MagicMock())
        result = classify_reply("yeah tmrw around 2ish")
        assert result["intent"] == "unclear"
        assert result["raw_text"] == "yeah tmrw around 2ish"


# ── Error handling: timeout → unclear ────────────────────────────────────

def test_openai_timeout_returns_unclear():
    with patch("classifier.openai.OpenAI") as MockClient:
        instance = MockClient.return_value
        instance.chat.completions.create.side_effect = openai.APITimeoutError(request=MagicMock())
        result = classify_reply("can be there wednesday morning")
        assert result["intent"] == "unclear"


# ── Error handling: malformed JSON from LLM → unclear ────────────────────

def test_openai_bad_json_returns_unclear():
    message = MagicMock()
    message.content = "this is not json"
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]

    with patch("classifier.openai.OpenAI") as MockClient:
        instance = MockClient.return_value
        instance.chat.completions.create.return_value = response
        result = classify_reply("ill try but no promises")
        assert result["intent"] == "unclear"


# ── Edge: invalid intent from LLM → unclear ─────────────────────────────

def test_invalid_intent_coerced_to_unclear():
    payload = {"intent": "maybe", "time": None, "reason": None, "condition": None}
    with patch("classifier.openai.OpenAI") as MockClient:
        instance = MockClient.return_value
        instance.chat.completions.create.return_value = _mock_openai_response(payload)
        result = classify_reply("hmmm let me think")
        assert result["intent"] == "unclear"


# ── Result structure ─────────────────────────────────────────────────────

def test_result_has_all_keys():
    result = classify_reply("yes")
    assert set(result.keys()) == {"intent", "time", "reason", "condition", "raw_text"}
