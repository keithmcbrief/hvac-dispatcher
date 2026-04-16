"""Core state machine engine for Eddie's HVAC Contractor Dispatch Agent.

Orchestrates the entire contractor coordination workflow: dispatching jobs,
processing contractor replies, follow-ups, escalation, and the background
polling loop.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta

import config
import db as db_module
import sms
from classifier import classify_reply

logger = logging.getLogger(__name__)

_last_heartbeat_alert_at: datetime | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _format_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _next_action_time() -> str:
    return _format_dt(_now_utc() + timedelta(seconds=config.FOLLOW_UP_INTERVAL_SECONDS))


def _should_send_heartbeat_alert(now: datetime, last_created: str | None) -> bool:
    """Return True when the no-new-jobs heartbeat should alert."""
    global _last_heartbeat_alert_at

    if config.HEARTBEAT_HOURS <= 0 or not last_created:
        return False

    try:
        last_dt = datetime.strptime(last_created, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        logger.warning("Invalid last job timestamp: %s", last_created)
        return False

    if (now - last_dt) <= timedelta(hours=config.HEARTBEAT_HOURS):
        return False

    if (
        config.HEARTBEAT_ALERT_INTERVAL_HOURS > 0
        and _last_heartbeat_alert_at is not None
        and (now - _last_heartbeat_alert_at)
        < timedelta(hours=config.HEARTBEAT_ALERT_INTERVAL_HOURS)
    ):
        return False

    _last_heartbeat_alert_at = now
    return True


def _contractors_by_priority() -> list[dict]:
    """Return contractor list sorted by priority (ascending)."""
    return sorted(
        [{"name": name, **info} for name, info in config.CONTRACTORS.items()],
        key=lambda c: c["priority"],
    )


def _build_eddie_summary(conn, job, contractor_name=None, time_display=None) -> str:
    """Build a concise summary for Eddie."""
    lines = []
    lines.append(f"Job #{job['id']}: {job['service_type'] or 'HVAC'}")
    lines.append(f"Customer: {job['customer_name']} ({job['phone']})")
    lines.append(f"Address: {job['address']}")
    if contractor_name:
        lines.append(f"Assigned: {contractor_name}")
    if time_display and time_display != "not specified":
        lines.append(f"ETA: {time_display}")
    return "\n".join(lines)


def _build_job_sms(job) -> str:
    """Build the initial job SMS text."""
    lines = [f"New Job (#{job['id']})"]
    lines.append(f"Service: {job['service_type'] or 'N/A'}")
    lines.append(f"Address: {job['address'] or 'N/A'}")
    lines.append(f"Customer: {job['customer_name'] or 'N/A'}")
    lines.append(f"Issue: {job['issue_description'] or 'N/A'}")
    lines.append("")
    lines.append("When can you be there?")
    return "\n".join(lines)


def _eta_from_reply_text(text: str) -> str | None:
    """Extract a usable ETA from short contractor replies."""
    stripped = (text or "").strip()
    lower = stripped.lower()
    if not stripped:
        return None

    if re.search(r"\b(omw|on my way|on the way|heading (over|there|out)|en route)\b", lower):
        return "on the way"

    if re.search(r"\b(1[0-2]|0?[1-9])(?::[0-5]\d)?\s*(am|pm)\b", lower):
        return stripped

    if re.search(r"\b(in|about|around)\s+\d+\s*(min|mins|minutes|hour|hours|hr|hrs)\b", lower):
        return stripped

    if re.search(r"\b(today|tomorrow|tonight)\b", lower) and re.search(
        r"\b(morning|afternoon|evening|night|noon|[1-9]|1[0-2])\b", lower
    ):
        return stripped

    return None


def _confirmed_time_from_result(result: dict) -> str | None:
    """Return the ETA that is safe to send to the customer."""
    extracted_time = (result.get("time") or "").strip()
    if extracted_time:
        return extracted_time
    return _eta_from_reply_text(result.get("raw_text", ""))


def _build_eta_request_sms(job) -> str:
    return (
        f"Thanks. What time can you arrive for Job #{job['id']}? "
        'Please reply with an ETA like "5pm" or "in 45 minutes".'
    )


def _build_customer_confirmation_sms(job, contractor_name: str, time_display: str) -> str:
    service = job["service_type"] or "service request"
    if time_display.lower() == "on the way":
        return (
            f"{config.BUSINESS_NAME}: {contractor_name} is on the way for your "
            f"{service} at {job['address']}. Reply here if anything changes."
        )
    return (
        f"{config.BUSINESS_NAME}: {contractor_name} is confirmed for {time_display} "
        f"for your {service} at {job['address']}. Reply here if anything changes."
    )


def _customer_sms_failure_reason(exc: Exception) -> str:
    """Return a Slack-safe explanation for customer SMS send failures."""
    code = getattr(exc, "code", None)
    msg = (getattr(exc, "msg", "") or str(exc)).strip()

    if code == 21211:
        return "Twilio rejected the customer phone number as invalid (error 21211)."

    if code:
        return f"Twilio rejected the customer text (error {code}): {msg}"

    if msg:
        return f"Customer text send failed: {msg}"

    return "Customer text send failed for an unknown reason."


def _send_and_log(conn, job_id, contractor_name, phone, body):
    """Send SMS and log the outbound message. Returns the twilio SID."""
    try:
        sid = sms.send_sms(phone, body)
        db_module.log_message(
            conn, job_id, "outbound", body,
            contractor_name=contractor_name, twilio_sid=sid,
        )
        return sid
    except Exception:
        logger.exception("Failed to send SMS to %s for job %s", contractor_name, job_id)
        raise


def _notify_eddie(conn, job_id, body):
    """Send notification to Eddie (Slack or SMS) and log it to the messages table."""
    sid = None
    if config.SLACK_ENABLED:
        import slack as slack_module
        slack_module.send_slack_message(body)
    else:
        sid = sms.send_eddie_notification(body)
    db_module.log_message(
        conn, job_id, "outbound", body,
        contractor_name="Eddie", twilio_sid=sid,
    )
    return sid


def _notify_customer_confirmed(conn, job, contractor_name: str, time_display: str) -> tuple[bool, str, str | None]:
    """Text the customer with contractor confirmation details."""
    body = _build_customer_confirmation_sms(job, contractor_name, time_display)
    if not job["phone"]:
        logger.warning("No customer phone for job %s", job["id"])
        return False, body, "No customer phone number is available."

    try:
        sid = sms.send_sms(job["phone"], body)
        db_module.log_message(
            conn, job["id"], "outbound", body,
            contractor_name="Customer", twilio_sid=sid,
        )
        return True, body, None
    except Exception as exc:
        failure_reason = _customer_sms_failure_reason(exc)
        logger.exception("Failed to notify customer for job %s", job["id"])
        return False, body, failure_reason


# ---------------------------------------------------------------------------
# start_dispatch
# ---------------------------------------------------------------------------

def start_dispatch(conn, job_id):
    """Kick off contractor dispatch for a newly created job.

    Emergency: text ALL available contractors simultaneously.
    Normal: text the first available contractor in priority order.
    """
    job = db_module.get_job(conn, job_id)
    if job is None:
        logger.error("start_dispatch called with non-existent job_id=%s", job_id)
        return

    # Notify Eddie about the new incoming job
    priority_label = "🚨 EMERGENCY" if job["priority"] == "emergency" else "📞 New call"
    recording_line = f"\n🔊 Recording: {job['recording_url']}" if job["recording_url"] else ""
    transcript_block = ""
    if config.SLACK_ENABLED:
        import slack as slack_module
        transcript_block = slack_module.format_transcript_for_slack(job["transcript"])

    _notify_eddie(
        conn, job_id,
        f"{priority_label}\n\n"
        f"Job #{job['id']}: {job['service_type'] or 'HVAC'}\n"
        f"Customer: {job['customer_name']} ({job['phone']})\n"
        f"Address: {job['address']}\n"
        f"Issue: {job['issue_description'] or 'N/A'}"
        f"{recording_line}"
        f"{transcript_block}\n\n"
        f"Contacting contractors now."
    )

    body = _build_job_sms(job)
    contractors = _contractors_by_priority()

    if job["priority"] == "emergency":
        # Contact all contractors
        contacted = []
        for c in contractors:
            if not c["phone"]:
                continue
            try:
                _send_and_log(conn, job_id, c["name"], c["phone"], body)
                contacted.append(c["name"])
            except Exception:
                logger.exception("Failed to send emergency SMS to %s", c["name"])

        if not contacted:
            db_module.update_job(conn, job_id, status="no_contractor_available")
            _notify_eddie(
                conn, job_id,
                f"⚠️ Job #{job_id}: No contractors available for "
                f"{job['customer_name']} at {job['address']}. Please handle manually."
            )
            return

        # Use first contacted as current_contractor
        db_module.update_job(
            conn, job_id,
            status="contacting_contractor",
            current_contractor=contacted[0],
            attempt_count=1,
            next_action_at=_next_action_time(),
        )

    else:  # normal priority — always try Jose first
        for c in contractors:
            if not c["phone"]:
                continue
            try:
                _send_and_log(conn, job_id, c["name"], c["phone"], body)
            except Exception:
                logger.exception("Failed to send SMS to %s", c["name"])
                continue

            db_module.update_job(
                conn, job_id,
                status="contacting_contractor",
                current_contractor=c["name"],
                attempt_count=1,
                next_action_at=_next_action_time(),
            )
            return

        # No contractors available
        db_module.update_job(conn, job_id, status="no_contractor_available")
        _notify_eddie(
            conn, job_id,
            f"⚠️ Job #{job_id}: No contractors available for "
            f"{job['customer_name']} at {job['address']}. Please handle manually."
        )


# ---------------------------------------------------------------------------
# process_contractor_reply
# ---------------------------------------------------------------------------

def process_contractor_reply(conn, job, contractor_name, reply_text, twilio_message_sid=None):
    """Process an inbound SMS from a contractor for a given job."""
    if twilio_message_sid and db_module.get_message_by_twilio_message_sid(conn, twilio_message_sid):
        logger.info("Duplicate contractor SMS ignored: %s", twilio_message_sid)
        return

    result = classify_reply(reply_text)
    if job["status"] == "accepted_waiting_eta":
        eta = result.get("time") or _eta_from_reply_text(reply_text)
        if eta:
            result = {
                **result,
                "intent": "accepted",
                "time": eta,
                "raw_text": reply_text,
            }
    intent = result["intent"]

    # Log inbound message (twilio_message_sid used for dedup in production)
    db_module.log_message(
        conn, job["id"], "inbound", reply_text,
        contractor_name=contractor_name, parsed_intent=intent,
        twilio_message_sid=twilio_message_sid,
    )

    if intent == "accepted":
        _handle_accepted(conn, job, contractor_name, result)

    elif intent == "declined":
        escalate_to_next(conn, job)

    elif intent == "conditional":
        condition = result.get("condition") or reply_text
        db_module.update_job(
            conn, job["id"],
            status="conditional_pending",
            contractor_response=reply_text,
        )
        _notify_eddie(
            conn, job["id"],
            f"⚡ Job #{job['id']}: {contractor_name} replied with condition: "
            f"'{condition}'. Reply OK to confirm or NEXT to try another contractor."
        )

    elif intent == "unclear":
        _notify_eddie(
            conn, job["id"],
            f"❓ Job #{job['id']}: {contractor_name} sent unclear reply: "
            f"'{reply_text}'. Handle manually or wait for clearer response."
        )


def process_customer_reply(conn, job, from_number, reply_text, twilio_message_sid=None):
    """Relay an inbound customer SMS to Eddie/Slack with job context."""
    if twilio_message_sid and db_module.get_message_by_twilio_message_sid(conn, twilio_message_sid):
        logger.info("Duplicate customer SMS ignored: %s", twilio_message_sid)
        return

    db_module.log_message(
        conn, job["id"], "inbound", reply_text,
        contractor_name="Customer",
        twilio_message_sid=twilio_message_sid,
    )

    assigned = job["current_contractor"] or "not assigned"
    eta = job["confirmed_time"] or "not set"
    _notify_eddie(
        conn,
        job["id"],
        f"💬 Customer reply for Job #{job['id']}\n\n"
        f"Customer: {job['customer_name']} ({from_number or job['phone']})\n"
        f"Address: {job['address']}\n"
        f"Assigned: {assigned}\n"
        f"ETA: {eta}\n\n"
        f"Message:\n\"{reply_text}\""
    )


def _handle_accepted(conn, job, contractor_name, result):
    """Handle an accepted reply — confirm the job and notify Eddie."""
    confirmed_time = _confirmed_time_from_result(result)
    if not confirmed_time:
        _handle_accepted_missing_eta(conn, job, contractor_name, result)
        return

    time_display = confirmed_time

    db_module.update_job(
        conn, job["id"],
        status="contractor_confirmed",
        current_contractor=contractor_name,
        contractor_response=result.get("raw_text", ""),
        confirmed_time=confirmed_time,
        next_action_at=None,
    )

    # Refresh job to get latest data after update
    job = db_module.get_job(conn, job["id"])
    customer_sent, customer_body, customer_failure_reason = _notify_customer_confirmed(
        conn, job, contractor_name, time_display
    )
    summary = _build_eddie_summary(conn, job, contractor_name=contractor_name, time_display=time_display)
    customer_status = (
        f"Customer text sent:\n{customer_body}"
        if customer_sent
        else (
            f"Customer text was NOT sent: {customer_failure_reason}\n\n"
            f"Please contact the customer manually:\n{customer_body}"
        )
    )
    _notify_eddie(
        conn, job["id"],
        f"✓ CONFIRMED\n\n{summary}\n\n{customer_status}"
    )

    # Notify any other contractors who were contacted that the job is taken
    _notify_other_contractors_job_taken(conn, job, contractor_name)


def _handle_accepted_missing_eta(conn, job, contractor_name, result):
    """Ask the accepting contractor for an ETA before notifying the customer."""
    db_module.update_job(
        conn, job["id"],
        status="accepted_waiting_eta",
        current_contractor=contractor_name,
        contractor_response=result.get("raw_text", ""),
        next_action_at=None,
    )

    phone = config.CONTRACTORS.get(contractor_name, {}).get("phone")
    eta_request_sent = False
    if phone:
        try:
            _send_and_log(
                conn,
                job["id"],
                contractor_name,
                phone,
                _build_eta_request_sms(job),
            )
            eta_request_sent = True
        except Exception:
            logger.exception("Failed to request ETA from %s for job %s", contractor_name, job["id"])

    request_status = (
        f"Asked {contractor_name} for an ETA."
        if eta_request_sent
        else f"Could not text {contractor_name} for an ETA."
    )
    _notify_eddie(
        conn,
        job["id"],
        f"⚠️ Job #{job['id']}: {contractor_name} accepted but did not provide an ETA.\n\n"
        f"{request_status}\n"
        "Customer has not been texted yet."
    )


def _notify_other_contractors_job_taken(conn, job, accepted_contractor):
    """Notify other contacted contractors that someone else took the job."""
    rows = conn.execute(
        """SELECT DISTINCT contractor_name FROM messages
           WHERE job_id = ? AND direction = 'outbound'
             AND contractor_name != ? AND contractor_name != 'Eddie'""",
        (job["id"], accepted_contractor),
    ).fetchall()

    for row in rows:
        other_name = row["contractor_name"]
        phone = config.CONTRACTORS.get(other_name, {}).get("phone")
        if phone:
            body = (
                f"Update on Job #{job['id']} ({job['service_type']} at {job['address']}): "
                f"This job has been taken. No action needed. Thanks!"
            )
            try:
                _send_and_log(conn, job["id"], other_name, phone, body)
            except Exception:
                logger.exception("Failed to notify %s that job was taken", other_name)


# ---------------------------------------------------------------------------
# escalate_to_next
# ---------------------------------------------------------------------------

def escalate_to_next(conn, job):
    """Move to the next contractor in priority order."""
    contractors = _contractors_by_priority()
    current = job["current_contractor"]

    # Find current contractor's priority
    current_priority = None
    for c in contractors:
        if c["name"] == current:
            current_priority = c["priority"]
            break

    # Find next contractor after current in priority order
    for c in contractors:
        if current_priority is not None and c["priority"] <= current_priority:
            continue
        if not c["phone"]:
            continue

        body = _build_job_sms(job)
        try:
            _send_and_log(conn, job["id"], c["name"], c["phone"], body)
        except Exception:
            logger.exception("Failed to send SMS to %s during escalation", c["name"])
            continue

        db_module.update_job(
            conn, job["id"],
            status="contacting_contractor",
            current_contractor=c["name"],
            attempt_count=1,
            next_action_at=_next_action_time(),
        )

        _notify_eddie(
            conn, job["id"],
            f"🔄 Job #{job['id']}: {current} didn't respond. "
            f"Escalating to {c['name']}."
        )
        return

    # No more contractors
    db_module.update_job(conn, job["id"], status="no_contractor_available", next_action_at=None)
    _notify_eddie(
        conn, job["id"],
        f"⚠️ Job #{job['id']}: No contractors available for "
        f"{job['customer_name']} at {job['address']}. Please handle manually."
    )


# ---------------------------------------------------------------------------
# process_follow_up
# ---------------------------------------------------------------------------

def _get_unreplied_contractors(conn, job_id):
    """Return contractors who were texted but haven't replied for this job."""
    rows = conn.execute(
        """SELECT DISTINCT m.contractor_name FROM messages m
           WHERE m.job_id = ? AND m.direction = 'outbound'
             AND m.contractor_name != 'Eddie'
             AND m.contractor_name NOT IN (
               SELECT contractor_name FROM messages
               WHERE job_id = ? AND direction = 'inbound' AND contractor_name IS NOT NULL
             )""",
        (job_id, job_id),
    ).fetchall()
    return [r["contractor_name"] for r in rows]


def process_follow_up(conn, job):
    """Send a follow-up or escalate if max attempts reached."""
    attempt = job["attempt_count"]

    if attempt < config.MAX_ATTEMPTS_PER_CONTRACTOR:
        body = (
            f"Following up on Job #{job['id']}: {job['service_type']} at "
            f"{job['address']}. Can you make it?"
        )

        if job["priority"] == "emergency":
            # Emergency: follow up with ALL contractors who haven't replied
            unreplied = _get_unreplied_contractors(conn, job["id"])
            sent_to = []
            for name in unreplied:
                phone = config.CONTRACTORS.get(name, {}).get("phone")
                if not phone:
                    continue
                try:
                    _send_and_log(conn, job["id"], name, phone, body)
                    sent_to.append(name)
                except Exception:
                    logger.exception("Failed to send follow-up to %s", name)

            if sent_to:
                new_attempt = attempt + 1
                new_status = "follow_up_1" if new_attempt == 2 else "follow_up_2"
                db_module.update_job(
                    conn, job["id"],
                    status=new_status,
                    attempt_count=new_attempt,
                    next_action_at=_next_action_time(),
                )
                _notify_eddie(
                    conn, job["id"],
                    f"📩 Follow-up #{new_attempt} sent to {', '.join(sent_to)} for Job #{job['id']} "
                    f"({job['customer_name']} at {job['address']}). Still waiting for a reply."
                )
            else:
                # Everyone was already contacted and replied (or no one left)
                escalate_to_next(conn, job)
        else:
            # Normal: follow up with current contractor only
            contractor_name = job["current_contractor"]
            phone = config.CONTRACTORS.get(contractor_name, {}).get("phone")

            if not phone:
                logger.error("No phone found for contractor %s", contractor_name)
                escalate_to_next(conn, job)
                return

            try:
                _send_and_log(conn, job["id"], contractor_name, phone, body)
            except Exception:
                logger.exception("Failed to send follow-up to %s", contractor_name)
                return

            new_attempt = attempt + 1
            new_status = "follow_up_1" if new_attempt == 2 else "follow_up_2"
            db_module.update_job(
                conn, job["id"],
                status=new_status,
                attempt_count=new_attempt,
                next_action_at=_next_action_time(),
            )

            _notify_eddie(
                conn, job["id"],
                f"📩 Follow-up #{new_attempt} sent to {contractor_name} for Job #{job['id']} "
                f"({job['customer_name']} at {job['address']}). Still waiting for a reply."
            )
    else:
        # Max attempts reached — for emergency, no escalation needed (all were already texted)
        if job["priority"] == "emergency":
            db_module.update_job(conn, job["id"], status="no_contractor_available", next_action_at=None)
            _notify_eddie(
                conn, job["id"],
                f"⚠️ Job #{job['id']}: No contractors responded for "
                f"{job['customer_name']} at {job['address']}. Please handle manually."
            )
        else:
            escalate_to_next(conn, job)


# ---------------------------------------------------------------------------
# upgrade_to_emergency
# ---------------------------------------------------------------------------

def upgrade_to_emergency(conn, job):
    """Upgrade a job to emergency priority and contact additional contractors."""
    db_module.update_job(conn, job["id"], priority="emergency")

    # Find contractors already contacted for this job
    already_contacted = set()
    rows = conn.execute(
        """SELECT DISTINCT contractor_name FROM messages
           WHERE job_id = ? AND direction = 'outbound' AND contractor_name IS NOT NULL""",
        (job["id"],),
    ).fetchall()
    for row in rows:
        already_contacted.add(row["contractor_name"])

    # Find contractors who declined (check inbound messages with declined intent)
    declined = set()
    decline_rows = conn.execute(
        """SELECT DISTINCT contractor_name FROM messages
           WHERE job_id = ? AND direction = 'inbound' AND parsed_intent = 'declined'""",
        (job["id"],),
    ).fetchall()
    for row in decline_rows:
        declined.add(row["contractor_name"])

    contractors = _contractors_by_priority()
    # Refresh job to get current data
    job = db_module.get_job(conn, job["id"])
    body = _build_job_sms(job)

    new_contacts = []
    for c in contractors:
        if not c["phone"]:
            continue
        if c["name"] in already_contacted:
            continue
        if c["name"] in declined:
            continue

        try:
            _send_and_log(conn, job["id"], c["name"], c["phone"], body)
            new_contacts.append(c["name"])
        except Exception:
            logger.exception("Failed to send emergency upgrade SMS to %s", c["name"])

    if not new_contacts:
        _notify_eddie(conn, job["id"], "Already contacting all available contractors")


# ---------------------------------------------------------------------------
# process_eddie_command
# ---------------------------------------------------------------------------

def process_eddie_command(conn, job, command):
    """Process a command from Eddie: OK, NEXT, URGENT, CANCEL."""
    command = command.upper().strip()

    if command == "OK":
        if job["status"] == "conditional_pending":
            # Confirm the job as accepted
            contractor_name = job["current_contractor"]
            confirmed_time = job.get("confirmed_time") if hasattr(job, "get") else job["confirmed_time"]

            db_module.update_job(
                conn, job["id"],
                status="contractor_confirmed",
                confirmed_time=confirmed_time,
                next_action_at=None,
            )

            time_display = confirmed_time if confirmed_time else "not specified"
            job = db_module.get_job(conn, job["id"])
            summary = _build_eddie_summary(conn, job, contractor_name=contractor_name, time_display=time_display)
            _notify_eddie(
                conn, job["id"],
                f"✓ CONFIRMED\n\n{summary}\n\nPlease contact the customer to confirm the appointment."
            )

    elif command == "NEXT":
        if job["status"] in ("conditional_pending", "awaiting_reply", "accepted_waiting_eta"):
            escalate_to_next(conn, job)

    elif command == "URGENT":
        upgrade_to_emergency(conn, job)

    elif command == "CANCEL":
        db_module.update_job(conn, job["id"], status="cancelled", next_action_at=None)
        _notify_eddie(conn, job["id"], f"Job #{job['id']} cancelled.")


# ---------------------------------------------------------------------------
# run_polling_loop (async)
# ---------------------------------------------------------------------------

async def run_polling_loop(db_path):
    """Background polling loop. Runs every POLL_INTERVAL_SECONDS."""
    # Wait briefly for DB init to complete on startup
    await asyncio.sleep(1)
    while True:
        try:
            conn = db_module.get_connection()
            try:
                # 1. Process jobs needing action
                jobs = db_module.get_jobs_needing_action(conn)
                for job in jobs:
                    try:
                        if job["status"] == "send_failed":
                            # Retry sending
                            start_dispatch(conn, job["id"])
                        elif job["status"] in ("contacting_contractor", "awaiting_reply", "follow_up_1", "follow_up_2"):
                            process_follow_up(conn, job)
                    except Exception:
                        logger.exception("Error processing job %s", job["id"])

                # 2. Check for stale jobs
                stale = db_module.get_stale_jobs(conn, config.STALENESS_ALERT_MINUTES)
                for job in stale:
                    try:
                        sms.send_error_alert(
                            f"Stale job #{job['id']}: {job['status']} for "
                            f"{job['customer_name']} — no progress in "
                            f"{config.STALENESS_ALERT_MINUTES}+ minutes."
                        )
                    except Exception:
                        logger.exception("Error alerting stale job %s", job["id"])

                # 3. Auto-complete expired confirmed jobs
                expired = db_module.get_expired_confirmed_jobs(conn, config.JOB_TTL_HOURS)
                for job in expired:
                    try:
                        db_module.update_job(conn, job["id"], status="completed")
                        logger.info("Auto-completed expired job %s", job["id"])
                    except Exception:
                        logger.exception("Error auto-completing job %s", job["id"])

                # 4. Heartbeat check during business hours
                now = _now_utc()
                if 8 <= now.hour < 20:
                    last_created = db_module.get_last_job_created_at(conn)
                    if _should_send_heartbeat_alert(now, last_created):
                        sms.send_error_alert(
                            f"No new jobs in {config.HEARTBEAT_HOURS}+ hours. "
                            f"Is the system receiving calls?"
                        )

            finally:
                conn.close()

        except Exception:
            logger.exception("Polling loop error")
            try:
                sms.send_error_alert("Polling loop encountered an error. Check logs.")
            except Exception:
                logger.exception("Failed to send polling loop error alert")

        await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
