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


def _has_valid_retell_webhook_token(request: Request, path_token: str = "") -> bool:
    configured_token = config.RETELL_WEBHOOK_TOKEN
    supplied_token = path_token or request.query_params.get("token", "")
    return bool(
        configured_token
        and supplied_token
        and hmac.compare_digest(supplied_token, configured_token)
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

    data = json.loads(body_bytes)

    if config.SAVE_WEBHOOK_LOGS:
        os.makedirs("webhook_logs", exist_ok=True)
        log_file = f"webhook_logs/retell_{data.get('call', {}).get('call_id', 'unknown')}_{int(__import__('time').time())}.json"
        with open(log_file, "w") as f:
            json.dump(data, f, indent=2)
        logger.info("Retell webhook saved to %s", log_file)

    # Retell's real format puts extracted data in:
    #   call.call_analysis.custom_analysis_data.{caller_name, caller_phone, service_address, ...}
    # Also support flat test format and dynamic_variables for local testing.
    call_data = data.get("call", {})
    legacy_analysis = call_data.get("analysis", {})
    call_analysis = call_data.get("call_analysis", {})
    custom = call_analysis.get("custom_analysis_data", {})
    dynamic_vars = call_data.get("retell_llm_dynamic_variables", {})

    # Helper: check custom_analysis_data → dynamic_vars → legacy analysis → call_data → top-level
    # Tries multiple field name variants (Retell uses different names than our DB)
    def _extract(*fields, default=""):
        for field in fields:
            for source in (custom, dynamic_vars, legacy_analysis, call_analysis, call_data, data):
                val = source.get(field)
                if val:
                    return str(val) if val is not None else val
        return default

    customer_name = _extract("caller_name", "customer_name")
    phone = _extract("caller_phone", "phone", "from_number")
    address = _extract("service_address", "address")
    service_type = _extract("service_needed", "service_type", default=None)
    issue_description = _extract("Issue_description", "issue_description", default=None)
    retell_call_id = call_data.get("call_id") or data.get("call_id", "")
    transcript = call_data.get("transcript", "")
    recording_url = call_data.get("recording_url", "")

    # Normalize priority: Retell uses "urgency" with values like "Emergency"
    raw_urgency = _extract("urgency", "priority", default="normal")
    priority = "emergency" if raw_urgency.lower() in ("emergency", "urgent", "asap") else "normal"

    # Spam filter: skip dispatch if no address, but always notify Eddie
    is_lead = custom.get("is_lead", True)
    if is_lead is False or not address:
        logger.info("Skipping non-lead/spam call: %s", retell_call_id)
        reason = "no address provided" if not address else "not a lead"
        recording_url_val = call_data.get("recording_url", "")
        recording_line = f"\n🔊 Recording: {recording_url_val}" if recording_url_val else ""
        slack_module.send_slack_message(
            f"⚠️ Call received but NOT dispatched ({reason})\n\n"
            f"Customer: {customer_name or 'Unknown'} ({phone or 'no phone'})\n"
            f"Service: {service_type or 'N/A'}\n"
            f"Issue: {issue_description or 'N/A'}\n"
            f"Address: {address or 'NOT PROVIDED'}"
            f"{recording_line}"
        )
        return {"status": "skipped", "reason": reason}

    # Normalize phone: ensure it starts with +
    if phone and not phone.startswith("+"):
        phone = f"+{phone}"

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
    request_url = str(request.url)

    if not SKIP_SIGNATURE_VALIDATION and not sms.validate_twilio_signature(request_url, params, signature):
        return Response(content=TWIML_EMPTY, media_type="text/xml", status_code=403)

    from_number = params.get("From", "")
    body = params.get("Body", "")
    message_sid = params.get("MessageSid", "")

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
            if command_upper in ("OK", "NEXT", "URGENT", "CANCEL"):
                if job:
                    dispatch.process_eddie_command(conn, job, command_upper)
            else:
                sms.send_sms(
                    config.EDDIE_PHONE,
                    "Commands: OK, NEXT, URGENT, CANCEL. Prefix with JOB-{id} to target a specific job.",
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
    data = json.loads(body_bytes)

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
        if command_upper in ("OK", "NEXT", "URGENT", "CANCEL") and job:
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
            context={"request": request, "jobs": jobs, "slug": slug},
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

    def _extract(*fields, default=""):
        for field in fields:
            for source in (custom, dynamic_vars, call_analysis, call_data, data):
                val = source.get(field)
                if val:
                    return str(val) if val is not None else val
        return default

    customer_name = _extract("caller_name", "customer_name")
    phone = _extract("caller_phone", "phone", "from_number")
    address = _extract("service_address", "address")
    service_type = _extract("service_needed", "service_type", default=None)
    issue_description = _extract("Issue_description", "issue_description", default=None)
    retell_call_id = call_data.get("call_id") or data.get("call_id", "")

    raw_urgency = _extract("urgency", "priority", default="normal")
    priority = "emergency" if raw_urgency.lower() in ("emergency", "urgent", "asap") else "normal"

    is_lead = custom.get("is_lead", True)
    if is_lead is False or not address:
        return {"status": "skipped", "reason": "not a lead", "scenario": name}

    if phone and not phone.startswith("+"):
        phone = f"+{phone}"

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
