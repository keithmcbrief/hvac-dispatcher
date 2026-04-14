"""Core state machine engine for Eddie's HVAC Contractor Dispatch Agent.

Orchestrates the entire contractor coordination workflow: dispatching jobs,
processing contractor replies, follow-ups, escalation, and the background
polling loop.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

import config
import db as db_module
import sms
from classifier import classify_reply

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _format_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _next_action_time() -> str:
    return _format_dt(_now_utc() + timedelta(seconds=config.FOLLOW_UP_INTERVAL_SECONDS))


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
    result = classify_reply(reply_text)
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


def _handle_accepted(conn, job, contractor_name, result):
    """Handle an accepted reply — confirm the job and notify Eddie."""
    confirmed_time = result.get("time")
    time_display = confirmed_time if confirmed_time else "not specified"

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
    summary = _build_eddie_summary(conn, job, contractor_name=contractor_name, time_display=time_display)
    _notify_eddie(
        conn, job["id"],
        f"✓ CONFIRMED\n\n{summary}\n\nPlease contact the customer to confirm the appointment."
    )

    # Notify any other contractors who were contacted that the job is taken
    _notify_other_contractors_job_taken(conn, job, contractor_name)


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
        if job["status"] in ("conditional_pending", "awaiting_reply"):
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
                    if last_created:
                        last_dt = datetime.strptime(last_created, "%Y-%m-%d %H:%M:%S").replace(
                            tzinfo=timezone.utc
                        )
                        if (now - last_dt) > timedelta(hours=config.HEARTBEAT_HOURS):
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
