"""Reply classifier for HVAC contractor text messages.

Three-tier classification:
  Tier 1 — Regex for obvious yes/no replies (no LLM call).
  Tier 2 — Regex for common natural-language replies.
  Tier 3 — GPT-4o-mini for everything else.
"""

import json
import re

import openai

import config

# ── Tier 1 patterns ─────────────────────────────────────────────────────

_YES_PATTERNS: list[re.Pattern] = [
    re.compile(r"^\s*(yes|yeah|yep|yea|ya|ok|okay|sure|sounds good|i'?m in|on it)\s*[.!]*\s*$", re.IGNORECASE),
    re.compile(r"^\s*[👍✅]+\s*$"),
]

_NO_PATTERNS: list[re.Pattern] = [
    re.compile(
        r"^\s*(no|nah|can'?t|cannot|pass|busy|not available|unavailable)\s*[.!]*\s*$",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*[👎❌]+\s*$"),
]

_CONDITIONAL_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b(only if|if no one else|depends on|maybe,?\s+if|could but|can if|will if)\b", re.IGNORECASE),
]

_DECLINE_PATTERNS: list[re.Pattern] = [
    re.compile(
        r"\b(nah|no sorry|no way|sorry\s+can'?t|can'?t\b|cannot\b|booked solid|swamped|not gonna make it|try someone else)\b",
        re.IGNORECASE,
    ),
]

_ACCEPTANCE_PATTERNS: list[re.Pattern] = [
    re.compile(
        r"\b(yeah|yep|yes|sure thing|sure|i'?ll take it|i can do it|can be there|count me in|heading over|on my way|on the way)\b",
        re.IGNORECASE,
    ),
]

_TIME_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b(?:tmrw|tomorrow)\s+(?:at\s+)?(?:around\s+)?\d{1,2}(?::\d{2})?\s*(?:ish|am|pm)?\b", re.IGNORECASE),
    re.compile(r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+(?:morning|afternoon|evening|night|after lunch|before lunch|at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b", re.IGNORECASE),
    re.compile(r"\bby\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", re.IGNORECASE),
    re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", re.IGNORECASE),
    re.compile(r"\bheading over now\b", re.IGNORECASE),
    re.compile(r"\b(on my way|on the way)\b", re.IGNORECASE),
]

# ── Tier 3 system prompt ────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are classifying a text message from an HVAC contractor responding to a job request.\n"
    "Classify the message into exactly one category:\n"
    '- "accepted": The contractor agreed to take the job. Extract any mentioned time/date.\n'
    '- "declined": The contractor refused the job. Extract the reason if given.\n'
    '- "conditional": The contractor will take it under certain conditions. Extract the condition.\n'
    '- "unclear": The message doesn\'t clearly indicate acceptance, refusal, or conditions.\n'
    "\n"
    'Respond with JSON: {"intent": "...", "time": "...", "reason": "...", "condition": "..."}\n'
    "Use null for fields that don't apply."
)

_LLM_TIMEOUT_SECONDS = 10


# ── Public API ───────────────────────────────────────────────────────────

def classify_reply(text: str) -> dict:
    """Classify a contractor's text reply.

    Returns a dict with keys:
        intent   — "accepted" | "declined" | "conditional" | "unclear"
        time     — extracted time string or None
        reason   — decline reason or None
        condition — condition string or None
        raw_text — the original message
    """
    stripped = text.strip()

    # Tier 1: regex for obvious replies
    for pat in _YES_PATTERNS:
        if pat.match(stripped):
            return _result("accepted", raw_text=text)

    for pat in _NO_PATTERNS:
        if pat.match(stripped):
            return _result("declined", raw_text=text)

    deterministic = _classify_with_regex(text)
    if deterministic is not None:
        return deterministic

    # Tier 3: LLM classification
    return _classify_with_llm(text)


# ── Internals ────────────────────────────────────────────────────────────

def _result(
    intent: str,
    *,
    time: str | None = None,
    reason: str | None = None,
    condition: str | None = None,
    raw_text: str = "",
) -> dict:
    return {
        "intent": intent,
        "time": time,
        "reason": reason,
        "condition": condition,
        "raw_text": raw_text,
    }


def _classify_with_regex(text: str) -> dict | None:
    """Classify common contractor replies without depending on the LLM."""
    stripped = text.strip()
    if not stripped:
        return None

    for pat in _CONDITIONAL_PATTERNS:
        if pat.search(stripped):
            return _result("conditional", condition=stripped, raw_text=text)

    for pat in _DECLINE_PATTERNS:
        if pat.search(stripped):
            return _result("declined", reason=stripped, raw_text=text)

    for pat in _ACCEPTANCE_PATTERNS:
        if pat.search(stripped):
            time = _extract_time(stripped)
            if time:
                return _result("accepted", time=time, raw_text=text)

    return None


def _extract_time(text: str) -> str | None:
    for pat in _TIME_PATTERNS:
        match = pat.search(text)
        if not match:
            continue

        value = match.group(0).strip()
        if value.lower() == "heading over now":
            return "on the way"
        if value.lower().startswith("by "):
            return value[3:].strip()
        return value

    return None


def _classify_with_llm(text: str) -> dict:
    """Call GPT-4o-mini and parse its JSON response.

    Falls back to ``unclear`` on any error (network, timeout, bad JSON).
    """
    try:
        client = openai.OpenAI(api_key=config.OPENAI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            response_format={"type": "json_object"},
            timeout=_LLM_TIMEOUT_SECONDS,
        )
        content = response.choices[0].message.content
        data = json.loads(content)

        intent = data.get("intent", "unclear")
        if intent not in ("accepted", "declined", "conditional", "unclear"):
            intent = "unclear"

        return _result(
            intent,
            time=data.get("time"),
            reason=data.get("reason"),
            condition=data.get("condition"),
            raw_text=text,
        )
    except Exception:
        # Safe fallback — anything unexpected → human review
        return _result("unclear", raw_text=text)
