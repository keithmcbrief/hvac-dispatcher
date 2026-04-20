"""FastAPI application for Eddie's HVAC Contractor Dispatch Agent."""

import asyncio
import hmac
import json
import logging
import os
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import config
import db as db_module
import dispatch
import slack as slack_module
import sms

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format=json.dumps({
        "time": "%(asctime)s",
        "level": "%(levelname)s",
        "name": "%(name)s",
        "message": "%(message)s",
    }),
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Background task handle
# ---------------------------------------------------------------------------

_polling_task: asyncio.Task | None = None

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _polling_task
    db_module.init_db()
    _polling_task = asyncio.create_task(dispatch.run_polling_loop(config.DB_PATH))
    logger.info("Polling loop started")
    yield
    if _polling_task:
        _polling_task.cancel()
        try:
            await _polling_task
        except asyncio.CancelledError:
            pass
    logger.info("Polling loop stopped")


app = FastAPI(lifespan=lifespan)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(_BASE_DIR, "templates"))

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

TWIML_EMPTY = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'

SKIP_SIGNATURE_VALIDATION = os.getenv("SKIP_SIGNATURE_VALIDATION", "").lower() in ("true", "1", "yes")

_BLANKISH_VALUES = {"", "n/a", "na", "none", "null", "unknown", "not provided"}
_HVAC_KEYWORDS = (
    "ac",
    "a/c",
    "air conditioner",
    "air conditioning",
    "air handler",
    "condenser",
    "compressor",
    "cooling",
    "duct",
    "evaporator",
    "freon",
    "furnace",
    "heat",
    "heater",
    "heating",
    "hvac",
    "mini split",
    "refrigerant",
    "thermostat",
    "unit",
    "vent",
)
_NON_HVAC_KEYWORDS = (
    "automobile",
    "auto",
    "car",
    "chevrolet",
    "dealer",
    "dealership",
    "trade in",
    "trade-in",
    "traded in",
    "truck",
    "vehicle",
    "westside chevrolet",
)
_OWNER_MESSAGE_PATTERNS = (
    r"\b(transfer|connect|speak|talk)\s+(to|with)\s+((mr\.?|mister)\s+)?(eddy|eddie|edilberto)\b",
    r"\b(ask(ing)?|look(ing)?)\s+for\s+((mr\.?|mister)\s+)?(eddy|eddie|edilberto)\b",
    r"\b(eddy|eddie|edilberto)\s+(berto|the owner)\b",
)


def _has_valid_retell_webhook_token(request: Request, path_token: str = "") -> bool:
    configured_token = config.RETELL_WEBHOOK_TOKEN
    supplied_token = path_token or request.query_params.get("token", "")
    return bool(
        configured_token
        and supplied_token
        and hmac.compare_digest(supplied_token, configured_token)
    )


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _case_get(source: dict, field: str):
    if field in source:
        return source[field]

    field_lower = field.lower()
    for key, value in source.items():
        if isinstance(key, str) and key.lower() == field_lower:
            return value
    return None


def _normalize_us_phone(phone: str) -> str:
    phone = phone.strip()
    if not phone or phone.startswith("+"):
        return phone

    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        return f"+1{digits}"
    if digits:
        return f"+{digits}"
    return phone


def _clean_text(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in _BLANKISH_VALUES:
        return ""
    return text


def _is_truthy(value) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    return str(value).strip().lower() in ("true", "1", "yes", "y")


def _is_falsey(value) -> bool:
    if value is False:
        return True
    if value is True or value is None:
        return False
    return str(value).strip().lower() in ("false", "0", "no", "n")


def _keyword_in_text(keyword: str, text: str) -> bool:
    if not keyword:
        return False
    if re.search(r"\W", keyword):
        return keyword in text
    return bool(re.search(rf"\b{re.escape(keyword)}\b", text))


def _has_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(_keyword_in_text(keyword, lower) for keyword in keywords)


def _has_dispatchable_service_address(address) -> bool:
    """Return True only for a usable service address, not just a city/state."""
    cleaned = _clean_text(address)
    if not cleaned:
        return False
    if re.fullmatch(
        r"[A-Za-z .'-]+,?\s+(?:[A-Z]{2}|[A-Za-z]+)(?:\s+\d{5}(?:-\d{4})?)?",
        cleaned,
        re.IGNORECASE,
    ):
        return False
    return bool(re.search(r"\b\d{1,6}\s+[A-Za-z]", cleaned))


def _caller_transcript_text(transcript) -> str:
    """Keep caller lines so the business greeting does not mask non-HVAC calls."""
    cleaned = _clean_text(transcript)
    if not cleaned:
        return ""

    caller_lines = []
    for line in cleaned.splitlines():
        if re.match(r"\s*(agent|assistant|kristi)\s*:", line, re.IGNORECASE):
            continue
        caller_lines.append(line)
    return "\n".join(caller_lines)


def _strip_speaker_prefix(text: str) -> str:
    return re.sub(r"^\s*(user|caller|customer)\s*:\s*", "", text, flags=re.IGNORECASE).strip()


def _truncate_text(text: str, max_chars: int = 360) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars - 3].rstrip()}..."


def _details_for_intent(service_type, issue_description, transcript, call_summary="") -> str:
    structured_details = " ".join(
        text
        for text in (
            _clean_text(service_type),
            _clean_text(issue_description),
            _clean_text(call_summary),
        )
        if text
    )
    caller_details = _caller_transcript_text(transcript)
    return " ".join(text for text in (structured_details, caller_details) if text)


def _is_owner_direct_request(service_type, issue_description, transcript, call_summary="") -> bool:
    details = _details_for_intent(service_type, issue_description, transcript, call_summary)
    return any(re.search(pattern, details, re.IGNORECASE) for pattern in _OWNER_MESSAGE_PATTERNS)


def _brief_owner_summary(issue_description, transcript, call_summary="") -> str:
    for candidate in (_clean_text(call_summary), _clean_text(issue_description)):
        if candidate:
            return _truncate_text(candidate)

    return _brief_caller_details(transcript) or "Caller asked to speak with Eddie."


def _brief_caller_details(transcript) -> str:
    caller_lines = [
        _strip_speaker_prefix(line)
        for line in _caller_transcript_text(transcript).splitlines()
        if _strip_speaker_prefix(line)
    ]
    return _truncate_text(" ".join(caller_lines))


def _build_owner_direct_text(
    customer_name,
    phone,
    service_type,
    issue_description,
    address,
    transcript,
    recording_url,
    call_summary="",
) -> str:
    lines = [
        "Caller asked for Eddie directly.",
        f"Caller: {customer_name or 'Unknown'} ({phone or 'no phone'})",
    ]
    summary = _brief_owner_summary(issue_description, transcript, call_summary)
    caller_details = _brief_caller_details(transcript)
    lines.append(f"Summary: {summary}")
    if caller_details and caller_details.lower() not in summary.lower():
        lines.append(f"Caller said: {caller_details}")

    if _clean_text(service_type):
        lines.append(f"Info: {_truncate_text(_clean_text(service_type), 160)}")
    if _clean_text(address):
        lines.append(f"Location/address: {_truncate_text(_clean_text(address), 160)}")
    if recording_url:
        lines.append(f"Recording: {recording_url}")

    return "\n".join(lines)


def _send_owner_direct_text(
    customer_name,
    phone,
    service_type,
    issue_description,
    address,
    transcript,
    recording_url,
    call_summary="",
) -> None:
    body = _build_owner_direct_text(
        customer_name,
        phone,
        service_type,
        issue_description,
        address,
        transcript,
        recording_url,
        call_summary,
    )
    try:
        sms.send_eddie_notification(body)
    except Exception:
        logger.exception("Failed to forward direct Eddie request by SMS")
        sms.send_error_alert("Failed to forward a direct Eddie request by SMS.")


def _non_dispatchable_reason(
    service_type,
    issue_description,
    transcript,
    call_summary="",
) -> str | None:
    """Catch clearly non-HVAC calls even when Retell marks them as a lead."""
    structured_details = _details_for_intent(service_type, issue_description, "", call_summary)
    details = _details_for_intent(service_type, issue_description, transcript, call_summary)
    if not details:
        return None

    structured_has_non_hvac = _has_keyword(structured_details, _NON_HVAC_KEYWORDS)
    structured_has_hvac = _has_keyword(structured_details, _HVAC_KEYWORDS)
    has_hvac = _has_keyword(details, _HVAC_KEYWORDS)
    has_non_hvac = _has_keyword(details, _NON_HVAC_KEYWORDS)

    if structured_has_non_hvac and not structured_has_hvac:
        return "not HVAC service"
    if has_non_hvac and not has_hvac:
        return "not HVAC service"
    return None


def _lead_status_skip_reason(lead_status) -> str | None:
    status = _clean_text(lead_status).lower().replace("-", "_").replace(" ", "_")
    if not status:
        return None
    if status in ("qualified_service_lead", "qualified_lead", "service_lead"):
        return None
    if status in ("not_a_lead", "not_lead", "non_lead"):
        return "not a lead"
    if status in ("needs_human_review", "human_review", "unclear", "unknown"):
        return "needs human review"
    return None


def _dispatch_skip_reason(
    *,
    is_lead,
    lead_status,
    hvac_service_request,
    dispatch_allowed,
    service_type,
    issue_description,
    transcript,
    call_summary,
    address,
) -> str | None:
    if _is_falsey(is_lead):
        return "not a lead"

    lead_status_reason = _lead_status_skip_reason(lead_status)
    if lead_status_reason:
        return lead_status_reason

    if _is_falsey(hvac_service_request):
        return "not HVAC service"

    non_dispatchable_reason = _non_dispatchable_reason(
        service_type,
        issue_description,
        transcript,
        call_summary,
    )
    if non_dispatchable_reason:
        return non_dispatchable_reason

    if not _has_dispatchable_service_address(address):
        return "no service address provided"

    if _is_falsey(dispatch_allowed):
        return "dispatch not allowed"

    return None


def _retell_payload_sources(data: dict) -> tuple[str, dict, dict, dict, tuple[dict, ...]]:
    root = _as_dict(data)
    wrapper = _as_dict(root.get("data"))

    call_data = _as_dict(root.get("call")) or _as_dict(wrapper.get("call"))
    if not call_data and any(
        key in wrapper for key in ("call_id", "call_analysis", "custom_analysis_data")
    ):
        call_data = wrapper
    if not call_data and any(
        key in root for key in ("call_id", "call_analysis", "custom_analysis_data")
    ):
        call_data = root

    call_analysis = (
        _as_dict(call_data.get("call_analysis"))
        or _as_dict(wrapper.get("call_analysis"))
        or _as_dict(root.get("call_analysis"))
    )
    custom = (
        _as_dict(call_analysis.get("custom_analysis_data"))
        or _as_dict(call_data.get("custom_analysis_data"))
        or _as_dict(wrapper.get("custom_analysis_data"))
        or _as_dict(root.get("custom_analysis_data"))
    )
    legacy_analysis = (
        _as_dict(call_data.get("analysis"))
        or _as_dict(wrapper.get("analysis"))
        or _as_dict(root.get("analysis"))
    )
    dynamic_vars = (
        _as_dict(call_data.get("retell_llm_dynamic_variables"))
        or _as_dict(wrapper.get("retell_llm_dynamic_variables"))
        or _as_dict(root.get("retell_llm_dynamic_variables"))
    )

    event_type = (
        root.get("event")
        or root.get("event_type")
        or wrapper.get("event")
        or wrapper.get("event_type")
        or ""
    )
    sources = (custom, dynamic_vars, legacy_analysis, call_analysis, call_data, wrapper, root)
    return str(event_type), call_data, call_analysis, custom, sources


def _extract_from_sources(sources: tuple[dict, ...], *fields, default=""):
    for field in fields:
        for source in sources:
            val = _case_get(source, field)
            if val is not None and val != "":
                return val
    return default


def _twilio_signature_url_candidates(request: Request) -> list[str]:
    url = str(request.url)
    candidates = [url]

    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    forwarded_host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    host = forwarded_host or request.headers.get("host", "")
    path = request.url.path
    query = f"?{request.url.query}" if request.url.query else ""

    for scheme in (forwarded_proto, "https"):
        if scheme and host:
            candidates.append(f"{scheme}://{host}{path}{query}")

    if url.startswith("http://"):
        candidates.append(f"https://{url[len('http://'):]}")

    unique_candidates = []
    for candidate in candidates:
        if candidate not in unique_candidates:
            unique_candidates.append(candidate)
    return unique_candidates


def _validate_twilio_request_signature(request: Request, params: dict, signature: str) -> bool:
    return any(
        sms.validate_twilio_signature(url, params, signature)
        for url in _twilio_signature_url_candidates(request)
    )


@app.get("/health")
async def health():
    return {"status": "healthy", "version": "1.0.0"}


@app.post("/webhook/retell")
@app.post("/webhook/retell/{webhook_token}")
async def webhook_retell(request: Request, webhook_token: str = ""):
    body_bytes = await request.body()
    signature = request.headers.get("x-retell-signature", "")

    if not SKIP_SIGNATURE_VALIDATION:
        if not sms.validate_retell_signature(body_bytes, signature, config.RETELL_API_KEY):
            if _has_valid_retell_webhook_token(request, webhook_token):
                logger.info("Retell webhook accepted by token fallback")
            else:
                _valid, signature_info = sms.validate_retell_signature_with_reason(
                    body_bytes,
                    signature,
                    config.RETELL_API_KEY,
                )
                signature_info["signature_header_names"] = [
                    key
                    for key in request.headers.keys()
                    if "signature" in key.lower() or "retell" in key.lower()
                ]
                signature_info["content_type"] = request.headers.get("content-type", "")
                signature_info["token_fallback_configured"] = bool(config.RETELL_WEBHOOK_TOKEN)
                signature_info["token_fallback_present"] = bool(
                    webhook_token or request.query_params.get("token", "")
                )
                logger.warning(
                    "Invalid Retell signature: %s",
                    json.dumps(signature_info, sort_keys=True),
                )
                return JSONResponse(status_code=403, content={"error": "Invalid signature"})

    try:
        data = json.loads(body_bytes)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})
    if not isinstance(data, dict):
        return JSONResponse(status_code=400, content={"error": "Invalid Retell payload"})

    if config.SAVE_WEBHOOK_LOGS:
        os.makedirs("webhook_logs", exist_ok=True)
        log_file = f"webhook_logs/retell_{data.get('call', {}).get('call_id', 'unknown')}_{int(__import__('time').time())}.json"
        with open(log_file, "w") as f:
            json.dump(data, f, indent=2)
        logger.info("Retell webhook saved to %s", log_file)

    event_type, call_data, call_analysis, custom, sources = _retell_payload_sources(data)

    # Helper: check custom_analysis_data -> dynamic_vars -> legacy analysis
    # -> call_analysis -> call_data -> wrapper/top-level data.
    def _extract(*fields, default=""):
        val = _extract_from_sources(sources, *fields, default=default)
        return str(val) if val is not None else val

    customer_name = _extract("caller_name", "customer_name")
    phone = _extract("caller_phone", "phone", "from_number")
    address = _extract("service_address", "address")
    service_type = _extract("service_needed", "service_type", default=None)
    issue_description = _extract("Issue_description", "issue_description", default=None)
    retell_call_id = _extract("call_id", default="")
    transcript = _extract("transcript", default="")
    recording_url = _extract("recording_url", default="")
    call_summary = _extract("call_summary", default="")
    hvac_service_request = _extract_from_sources(sources, "hvac_service_request", default=None)
    lead_status = _extract("lead_status", default="")
    owner_direct_request_value = _extract_from_sources(sources, "owner_direct_request", default=False)
    dispatch_allowed = _extract_from_sources(sources, "dispatch_allowed", default=None)

    # Normalize phone before any skip/forward path so Eddie gets a usable number.
    if phone and not phone.startswith("+"):
        phone = _normalize_us_phone(phone)

    if event_type and event_type != "call_analyzed" and not address:
        logger.info(
            "Ignoring Retell event before analysis: event=%s call_id=%s",
            event_type,
            retell_call_id,
        )
        return {
            "status": "ignored",
            "reason": "waiting_for_call_analyzed",
            "event": event_type,
        }

    # Normalize priority: Retell uses "urgency" with values like "Emergency"
    raw_urgency = _extract("urgency", "priority", default="normal")
    priority = "emergency" if raw_urgency.lower() in ("emergency", "urgent", "asap") else "normal"

    # Spam and safety filters: skip dispatch if this is not a real HVAC service
    # request or Retell only captured a city/state instead of a service address.
    is_lead = _extract_from_sources(sources, "is_lead", default=True)
    skip_reason = None
    owner_direct_request = _is_owner_direct_request(
        service_type,
        issue_description,
        transcript,
        call_summary,
    ) or _is_truthy(owner_direct_request_value)
    if owner_direct_request:
        _send_owner_direct_text(
            customer_name,
            phone,
            service_type,
            issue_description,
            address,
            transcript,
            recording_url,
            call_summary,
        )
        skip_reason = "not a lead"
    else:
        skip_reason = _dispatch_skip_reason(
            is_lead=is_lead,
            lead_status=lead_status,
            hvac_service_request=hvac_service_request,
            dispatch_allowed=dispatch_allowed,
            service_type=service_type,
            issue_description=issue_description,
            transcript=transcript,
            call_summary=call_summary,
            address=address,
        )

    if skip_reason:
        logger.info("Skipping non-lead/spam call: %s", retell_call_id)
        if not owner_direct_request:
            recording_line = f"\n🔊 Recording: {recording_url}" if recording_url else ""
            transcript_block = slack_module.format_transcript_for_slack(transcript)
            slack_module.send_slack_message(
                f"⚠️ Call received but NOT dispatched ({skip_reason})\n\n"
                f"Customer: {customer_name or 'Unknown'} ({phone or 'no phone'})\n"
                f"Service: {service_type or 'N/A'}\n"
                f"Issue: {issue_description or 'N/A'}\n"
                f"Address: {address or 'NOT PROVIDED'}"
                f"{recording_line}"
                f"{transcript_block}"
            )
        return {"status": "skipped", "reason": skip_reason}

    conn = db_module.get_connection()
    try:
        job = db_module.create_job(
            conn,
            customer_name=customer_name,
            phone=phone,
            address=address,
            service_type=service_type,
            issue_description=issue_description,
            priority=priority,
            retell_call_id=retell_call_id,
            transcript=transcript,
            recording_url=recording_url,
        )
        job_id = job["id"]

        # Check if this is a new job (not a dedup) by seeing if status is 'new'
        if job["status"] == "new":
            dispatch.start_dispatch(conn, job_id)

        return {"status": "ok", "job_id": job_id}
    finally:
        conn.close()


@app.post("/webhook/twilio")
async def webhook_twilio(request: Request):
    form_data = await request.form()
    params = dict(form_data)

    signature = request.headers.get("x-twilio-signature", "")

    if not SKIP_SIGNATURE_VALIDATION and not _validate_twilio_request_signature(request, params, signature):
        logger.warning(
            "Invalid Twilio signature: %s",
            json.dumps(
                {
                    "signature_present": bool(signature),
                    "signature_len": len(signature or ""),
                    "url_candidates": _twilio_signature_url_candidates(request),
                    "param_keys": sorted(params.keys()),
                    "forwarded_proto": request.headers.get("x-forwarded-proto", ""),
                    "forwarded_host_present": bool(request.headers.get("x-forwarded-host", "")),
                    "host": request.headers.get("host", ""),
                },
                sort_keys=True,
            ),
        )
        return Response(content=TWIML_EMPTY, media_type="text/xml", status_code=403)

    from_number = params.get("From", "")
    body = params.get("Body", "")
    message_sid = params.get("MessageSid", "")
    logger.info(
        "Twilio webhook accepted: %s",
        json.dumps(
            {
                "from_last4": from_number[-4:],
                "body_len": len(body),
                "message_sid_present": bool(message_sid),
                "known_contractor": from_number in config.CONTRACTOR_PHONES,
                "is_eddie": from_number == config.EDDIE_PHONE,
            },
            sort_keys=True,
        ),
    )

    conn = db_module.get_connection()
    try:
        if from_number in config.CONTRACTOR_PHONES:
            # Contractor reply — find which contractor this maps to
            contractor_name = config.CONTRACTOR_PHONES[from_number]
            job = db_module.get_active_job_for_contractor(conn, contractor_name)

            # If no active job under the mapped name, check if any contractor
            # sharing this phone number has an active job (handles test setups
            # where multiple contractors use the same number)
            if not job:
                for name, info in config.CONTRACTORS.items():
                    if info["phone"] == from_number and name != contractor_name:
                        job = db_module.get_active_job_for_contractor(conn, name)
                        if job:
                            contractor_name = name
                            break

            if not job:
                logger.info(
                    "Inbound SMS from contractor %s but no active job", contractor_name
                )
                return Response(content=TWIML_EMPTY, media_type="text/xml")

            dispatch.process_contractor_reply(conn, job, contractor_name, body, twilio_message_sid=message_sid)

        elif from_number == config.EDDIE_PHONE:
            # Eddie command
            text = body.strip()
            job = None

            # Parse optional JOB-{id} prefix
            match = re.match(r"JOB-(\d+)\s+(.*)", text, re.IGNORECASE)
            if match:
                job_id = int(match.group(1))
                command = match.group(2).strip()
                job = db_module.get_job(conn, job_id)
            else:
                command = text
                job = db_module.get_most_recent_active_job(conn)

            command_upper = command.upper().strip()
            if command_upper in (
                "OK",
                "NEXT",
                "URGENT",
                "CANCEL",
            ):
                if job:
                    dispatch.process_eddie_command(conn, job, command_upper)
            else:
                sms.send_sms(
                    config.EDDIE_PHONE,
                    "Commands: OK, NEXT, URGENT, CANCEL. Prefix with JOB-{id} to target a specific job.",
                )

        else:
            customer_phone = _normalize_us_phone(from_number)
            job = db_module.get_most_recent_job_for_customer_phone(conn, customer_phone)
            if job:
                dispatch.process_customer_reply(
                    conn,
                    job,
                    customer_phone,
                    body,
                    twilio_message_sid=message_sid,
                )
            else:
                logger.info("Inbound SMS from unknown number %s — ignoring", from_number)

        return Response(content=TWIML_EMPTY, media_type="text/xml")
    finally:
        conn.close()


@app.post("/webhook/slack")
async def webhook_slack(request: Request):
    """Handle Slack Events API messages (Eddie's commands)."""
    body_bytes = await request.body()
    try:
        data = json.loads(body_bytes)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "Invalid Slack payload"})

    # Slack URL verification challenge (one-time setup)
    if data.get("type") == "url_verification":
        return JSONResponse(content={"challenge": data["challenge"]})

    # Validate signature
    if not SKIP_SIGNATURE_VALIDATION:
        timestamp = request.headers.get("x-slack-request-timestamp", "")
        signature = request.headers.get("x-slack-signature", "")
        if not slack_module.validate_slack_request(timestamp, body_bytes, signature):
            return JSONResponse(status_code=403, content={"error": "Invalid signature"})

    # Only handle message events (not bot messages to avoid loops)
    event = data.get("event", {})
    if event.get("type") != "message" or event.get("subtype") or event.get("bot_id"):
        return {"status": "ignored"}

    text = event.get("text", "").strip()
    if not text:
        return {"status": "ignored"}

    # Parse command — same logic as Eddie SMS handler
    conn = db_module.get_connection()
    try:
        job = None
        match = re.match(r"JOB-(\d+)\s+(.*)", text, re.IGNORECASE)
        if match:
            job_id = int(match.group(1))
            command = match.group(2).strip()
            job = db_module.get_job(conn, job_id)
        else:
            command = text
            job = db_module.get_most_recent_active_job(conn)

        command_upper = command.upper().strip()
        if command_upper in (
            "OK",
            "NEXT",
            "URGENT",
            "CANCEL",
        ) and job:
            dispatch.process_eddie_command(conn, job, command_upper)
            return {"status": "ok", "job_id": job["id"], "command": command_upper}

        return {"status": "ignored", "reason": "not a command"}
    finally:
        conn.close()


@app.get("/dash/{slug}", response_class=HTMLResponse)
async def dashboard(request: Request, slug: str):
    if slug != config.DASHBOARD_SLUG:
        return Response(status_code=404)

    conn = db_module.get_connection()
    try:
        jobs = db_module.get_recent_jobs(conn, 50)
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "request": request,
                "jobs": jobs,
                "slug": slug,
                "dry_run": config.DRY_RUN,
                "slack_enabled": config.SLACK_ENABLED,
            },
        )
    finally:
        conn.close()


@app.post("/dash/{slug}/urgent/{job_id}")
async def dashboard_urgent(slug: str, job_id: int):
    if slug != config.DASHBOARD_SLUG:
        return Response(status_code=404)

    conn = db_module.get_connection()
    try:
        job = db_module.get_job(conn, job_id)
        if job:
            dispatch.upgrade_to_emergency(conn, job)
        return RedirectResponse(url=f"/dash/{slug}", status_code=303)
    finally:
        conn.close()


@app.post("/dash/{slug}/cancel/{job_id}")
async def dashboard_cancel(slug: str, job_id: int):
    if slug != config.DASHBOARD_SLUG:
        return Response(status_code=404)

    conn = db_module.get_connection()
    try:
        job = db_module.get_job(conn, job_id)
        if job:
            dispatch.process_eddie_command(conn, job, "CANCEL")
        return RedirectResponse(url=f"/dash/{slug}", status_code=303)
    finally:
        conn.close()


@app.get("/dash/{slug}/messages/{job_id}")
async def job_messages(slug: str, job_id: int):
    """Return all messages for a job as JSON (used by dashboard)."""
    if slug != config.DASHBOARD_SLUG:
        return Response(status_code=404)

    conn = db_module.get_connection()
    try:
        rows = conn.execute(
            "SELECT id, job_id, direction, contractor_name, body, parsed_intent, twilio_sid, timestamp FROM messages WHERE job_id = ? ORDER BY id ASC",
            (job_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


@app.get("/dash/{slug}/all-messages")
async def all_messages(slug: str):
    """Return all messages grouped by person (contractor or Eddie)."""
    if slug != config.DASHBOARD_SLUG:
        return Response(status_code=404)

    conn = db_module.get_connection()
    try:
        rows = conn.execute(
            """SELECT m.id, m.job_id, m.direction, m.contractor_name, m.body,
                      m.parsed_intent, m.twilio_sid, m.timestamp,
                      j.customer_name, j.address, j.service_type, j.status as job_status
               FROM messages m
               LEFT JOIN jobs j ON m.job_id = j.id
               ORDER BY m.id ASC""",
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Dry-run test endpoints (only available when DRY_RUN=true)
# ---------------------------------------------------------------------------

@app.get("/test/scenarios")
async def list_scenarios():
    """List available test scenarios. DRY_RUN mode only."""
    if not config.DRY_RUN:
        return JSONResponse(status_code=403, content={"error": "Only available in DRY_RUN mode"})

    scenario_dir = os.path.join(_BASE_DIR, "test_scenarios")
    scenarios = []
    if os.path.isdir(scenario_dir):
        for f in sorted(os.listdir(scenario_dir)):
            if f.endswith(".json"):
                scenarios.append(f.replace(".json", ""))
    return scenarios


@app.post("/test/scenario/{name}")
async def fire_scenario(name: str):
    """Fire a test scenario by name. DRY_RUN mode only."""
    if not config.DRY_RUN:
        return JSONResponse(status_code=403, content={"error": "Only available in DRY_RUN mode"})

    scenario_file = os.path.join(_BASE_DIR, "test_scenarios", f"{name}.json")
    if not os.path.isfile(scenario_file):
        return JSONResponse(status_code=404, content={"error": f"Scenario '{name}' not found"})

    with open(scenario_file) as f:
        data = json.load(f)

    # Feed it through the same webhook handler logic
    call_data = data.get("call", {})
    call_analysis = call_data.get("call_analysis", {})
    custom = call_analysis.get("custom_analysis_data", {})
    dynamic_vars = call_data.get("retell_llm_dynamic_variables", {})
    sources = (custom, dynamic_vars, call_analysis, call_data, data)

    def _extract(*fields, default=""):
        val = _extract_from_sources(sources, *fields, default=default)
        return str(val) if val is not None else val

    customer_name = _extract("caller_name", "customer_name")
    phone = _extract("caller_phone", "phone", "from_number")
    address = _extract("service_address", "address")
    service_type = _extract("service_needed", "service_type", default=None)
    issue_description = _extract("Issue_description", "issue_description", default=None)
    retell_call_id = call_data.get("call_id") or data.get("call_id", "")
    transcript = _extract("transcript", default="")
    call_summary = _extract("call_summary", default="")
    hvac_service_request = _extract_from_sources(sources, "hvac_service_request", default=None)
    lead_status = _extract("lead_status", default="")
    owner_direct_request_value = _extract_from_sources(sources, "owner_direct_request", default=False)
    dispatch_allowed = _extract_from_sources(sources, "dispatch_allowed", default=None)

    raw_urgency = _extract("urgency", "priority", default="normal")
    priority = "emergency" if raw_urgency.lower() in ("emergency", "urgent", "asap") else "normal"

    if phone and not phone.startswith("+"):
        phone = _normalize_us_phone(phone)

    is_lead = custom.get("is_lead", True)
    skip_reason = None
    owner_direct_request = _is_owner_direct_request(
        service_type,
        issue_description,
        transcript,
        call_summary,
    ) or _is_truthy(owner_direct_request_value)
    if owner_direct_request:
        _send_owner_direct_text(
            customer_name,
            phone,
            service_type,
            issue_description,
            address,
            transcript,
            "",
            call_summary,
        )
        skip_reason = "not a lead"
    else:
        skip_reason = _dispatch_skip_reason(
            is_lead=is_lead,
            lead_status=lead_status,
            hvac_service_request=hvac_service_request,
            dispatch_allowed=dispatch_allowed,
            service_type=service_type,
            issue_description=issue_description,
            transcript=transcript,
            call_summary=call_summary,
            address=address,
        )

    if skip_reason:
        return {"status": "skipped", "reason": skip_reason, "scenario": name}

    conn = db_module.get_connection()
    try:
        job = db_module.create_job(
            conn,
            customer_name=customer_name,
            phone=phone,
            address=address,
            service_type=service_type,
            issue_description=issue_description,
            priority=priority,
            retell_call_id=retell_call_id,
        )
        job_id = job["id"]
        if job["status"] == "new":
            dispatch.start_dispatch(conn, job_id)
        return {"status": "ok", "job_id": job_id, "scenario": name}
    finally:
        conn.close()


@app.post("/test/clear")
async def clear_db():
    """Clear all jobs and messages. DRY_RUN mode only."""
    if not config.DRY_RUN:
        return JSONResponse(status_code=403, content={"error": "Only available in DRY_RUN mode"})

    conn = db_module.get_connection()
    try:
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM jobs")
        conn.commit()
        return {"status": "ok"}
    finally:
        conn.close()


@app.post("/test/reply")
async def test_reply(request: Request):
    """Simulate a contractor SMS reply. DRY_RUN mode only."""
    if not config.DRY_RUN:
        return JSONResponse(status_code=403, content={"error": "Only available in DRY_RUN mode"})

    data = await request.json()
    contractor_name = data.get("contractor")
    body = data.get("body", "")
    job_id = data.get("job_id")

    if not contractor_name or not body:
        return JSONResponse(status_code=400, content={"error": "contractor and body required"})

    conn = db_module.get_connection()
    try:
        if job_id:
            job = db_module.get_job(conn, job_id)
        else:
            job = db_module.get_active_job_for_contractor(conn, contractor_name)

        if not job:
            return JSONResponse(status_code=404, content={"error": f"No active job for {contractor_name}"})

        dispatch.process_contractor_reply(conn, job, contractor_name, body)
        return {"status": "ok", "job_id": job["id"], "contractor": contractor_name}
    finally:
        conn.close()


@app.post("/test/customer")
async def test_customer_reply(request: Request):
    """Simulate a customer SMS reply. DRY_RUN mode only."""
    if not config.DRY_RUN:
        return JSONResponse(status_code=403, content={"error": "Only available in DRY_RUN mode"})

    data = await request.json()
    body = data.get("body", "")
    job_id = data.get("job_id")
    phone = data.get("phone", "")

    if not body:
        return JSONResponse(status_code=400, content={"error": "body required"})

    customer_phone = _normalize_us_phone(phone) if phone else ""
    conn = db_module.get_connection()
    try:
        if job_id:
            job = db_module.get_job(conn, job_id)
        elif customer_phone:
            job = db_module.get_most_recent_job_for_customer_phone(conn, customer_phone)
        else:
            job = db_module.get_most_recent_active_job(conn)

        if not job:
            return JSONResponse(status_code=404, content={"error": "No customer job found"})

        dispatch.process_customer_reply(
            conn,
            job,
            customer_phone or job["phone"],
            body,
        )
        return {"status": "ok", "job_id": job["id"], "phone": customer_phone or job["phone"]}
    finally:
        conn.close()


@app.post("/test/eddie")
async def test_eddie(request: Request):
    """Simulate an Eddie command. DRY_RUN mode only."""
    if not config.DRY_RUN:
        return JSONResponse(status_code=403, content={"error": "Only available in DRY_RUN mode"})

    data = await request.json()
    command = data.get("command", "").upper().strip()
    job_id = data.get("job_id")

    if command not in ("OK", "NEXT", "URGENT", "CANCEL"):
        return JSONResponse(status_code=400, content={"error": "command must be OK, NEXT, URGENT, or CANCEL"})

    conn = db_module.get_connection()
    try:
        if job_id:
            job = db_module.get_job(conn, job_id)
        else:
            job = db_module.get_most_recent_active_job(conn)

        if not job:
            return JSONResponse(status_code=404, content={"error": "No active job found"})

        dispatch.process_eddie_command(conn, job, command)
        return {"status": "ok", "job_id": job["id"], "command": command}
    finally:
        conn.close()
