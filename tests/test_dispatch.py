"""Tests for the dispatch state machine."""

import importlib
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock, call

import pytest


@pytest.fixture(autouse=True)
def _use_temp_db(monkeypatch, tmp_path):
    """Point DB_PATH to a temp file and reinitialise for every test."""
    db_file = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_file)

    # Set contractor phones for tests
    monkeypatch.setenv("JOSE_PHONE", "+15550001111")
    monkeypatch.setenv("MARIO_PHONE", "+15550002222")
    monkeypatch.setenv("RAUL_PHONE", "+15550003333")
    monkeypatch.setenv("EDDIE_PHONE", "+15550009999")
    monkeypatch.setenv("BUILDER_PHONE", "+15550008888")
    monkeypatch.setenv("SLACK_ENABLED", "false")

    import config
    importlib.reload(config)

    import db
    importlib.reload(db)

    db.init_db()
    yield db


@pytest.fixture
def conn(_use_temp_db):
    c = _use_temp_db.get_connection()
    yield c
    c.close()


@pytest.fixture
def db(_use_temp_db):
    return _use_temp_db


@pytest.fixture
def dispatch_module(_use_temp_db):
    """Reload dispatch module after config/db are set up."""
    import dispatch
    importlib.reload(dispatch)
    return dispatch


def _create_test_job(db, conn, priority="normal"):
    """Helper to create a standard test job."""
    return db.create_job(
        conn,
        customer_name="John Smith",
        phone="+15551234567",
        address="123 Main St",
        service_type="AC repair",
        issue_description="No cold air",
        priority=priority,
    )


def test_start_dispatch_slack_notification_includes_transcript(dispatch_module, db, conn):
    dispatch_module.config.SLACK_ENABLED = True
    job = db.create_job(
        conn,
        customer_name="John Smith",
        phone="+15551234567",
        address="123 Main St",
        service_type="AC repair",
        issue_description="No cold air",
        transcript="Agent: Hi\nUser: My AC is broken",
    )

    with patch("slack.send_slack_message") as mock_slack, patch("dispatch.sms") as mock_sms:
        mock_sms.send_sms.return_value = "SM_TEST_SID"
        dispatch_module.start_dispatch(conn, job["id"])

    slack_text = mock_slack.call_args[0][0]
    assert "Full transcript:" in slack_text
    assert "Agent: Hi\nUser: My AC is broken" in slack_text


# ---------------------------------------------------------------------------
# start_dispatch — normal priority
# ---------------------------------------------------------------------------


@patch("dispatch.sms")
def test_start_dispatch_normal_contacts_jose_first(mock_sms, dispatch_module, db, conn):
    """Normal priority should contact Jose first (priority 1)."""
    mock_sms.send_sms.return_value = "SM_TEST_SID"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"

    job = _create_test_job(db, conn)
    dispatch_module.start_dispatch(conn, job["id"])

    # Should have sent SMS to Jose
    mock_sms.send_sms.assert_called_once()
    call_args = mock_sms.send_sms.call_args
    assert call_args[0][0] == "+15550001111"  # Jose's phone
    assert f"#{job['id']}" in call_args[0][1]

    # Job should be updated
    updated = db.get_job(conn, job["id"])
    assert updated["status"] == "contacting_contractor"
    assert updated["current_contractor"] == "Jose"
    assert updated["attempt_count"] == 1
    assert updated["next_action_at"] is not None


# ---------------------------------------------------------------------------
# start_dispatch — emergency
# ---------------------------------------------------------------------------


@patch("dispatch.sms")
def test_start_dispatch_emergency_contacts_all(mock_sms, dispatch_module, db, conn):
    """Emergency should contact all available contractors."""
    mock_sms.send_sms.return_value = "SM_TEST_SID"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"

    job = _create_test_job(db, conn, priority="emergency")
    dispatch_module.start_dispatch(conn, job["id"])

    # Should have sent to all 3 contractors
    assert mock_sms.send_sms.call_count == 3

    phones_called = [c[0][0] for c in mock_sms.send_sms.call_args_list]
    assert "+15550001111" in phones_called  # Jose
    assert "+15550002222" in phones_called  # Mario
    assert "+15550003333" in phones_called  # Raul

    updated = db.get_job(conn, job["id"])
    assert updated["status"] == "contacting_contractor"
    assert updated["current_contractor"] == "Jose"  # first contacted


# ---------------------------------------------------------------------------
# start_dispatch — always texts Jose first even if he has another job
# ---------------------------------------------------------------------------


@patch("dispatch.sms")
def test_start_dispatch_texts_jose_even_if_busy(mock_sms, dispatch_module, db, conn):
    """Normal priority should always text Jose first regardless of other active jobs."""
    mock_sms.send_sms.return_value = "SM_TEST_SID"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"

    # Give Jose an active job
    busy_job = db.create_job(conn, "Other Customer", "+15559999999", "999 Other St")
    db.update_job(conn, busy_job["id"], current_contractor="Jose", status="awaiting_reply")

    job = _create_test_job(db, conn)
    dispatch_module.start_dispatch(conn, job["id"])

    # Should still text Jose (priority 1, always first)
    call_args = mock_sms.send_sms.call_args
    assert call_args[0][0] == "+15550001111"  # Jose's phone

    updated = db.get_job(conn, job["id"])
    assert updated["current_contractor"] == "Jose"


# ---------------------------------------------------------------------------
# start_dispatch — no contractors available (all SMS sends fail)
# ---------------------------------------------------------------------------


@patch("dispatch.sms")
def test_start_dispatch_no_contractors_when_all_sends_fail(mock_sms, dispatch_module, db, conn):
    """When all SMS sends fail, notify Eddie."""
    mock_sms.send_sms.side_effect = Exception("SMS failed")
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"

    job = _create_test_job(db, conn)
    dispatch_module.start_dispatch(conn, job["id"])

    updated = db.get_job(conn, job["id"])
    assert updated["status"] == "no_contractor_available"
    # Eddie gets 2 notifications: new call + no contractors available
    calls = [c[0][0] for c in mock_sms.send_eddie_notification.call_args_list]
    assert any("No contractors available" in c for c in calls)


# ---------------------------------------------------------------------------
# process_contractor_reply — accepted (normal)
# ---------------------------------------------------------------------------


@patch("dispatch.classify_reply")
@patch("dispatch.sms")
def test_reply_accepted_normal(mock_sms, mock_classify, dispatch_module, db, conn):
    """Accepted reply for normal job: confirm and notify Eddie."""
    mock_sms.send_sms.return_value = "SM_TEST_SID"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"
    mock_classify.return_value = {
        "intent": "accepted",
        "time": "2pm",
        "reason": None,
        "condition": None,
        "raw_text": "Yes I can be there at 2pm",
    }

    job = _create_test_job(db, conn)
    db.update_job(conn, job["id"], status="contacting_contractor", current_contractor="Jose")
    job = db.get_job(conn, job["id"])

    dispatch_module.process_contractor_reply(conn, job, "Jose", "Yes I can be there at 2pm")

    updated = db.get_job(conn, job["id"])
    assert updated["status"] == "contractor_confirmed"
    assert updated["confirmed_time"] == "2pm"
    assert updated["next_action_at"] is None

    mock_sms.send_eddie_notification.assert_called_once()
    notif = mock_sms.send_eddie_notification.call_args[0][0]
    assert "CONFIRMED" in notif
    assert "Jose" in notif
    assert "2pm" in notif


@patch("dispatch.classify_reply")
@patch("dispatch.sms")
def test_reply_accepted_texts_customer(mock_sms, mock_classify, dispatch_module, db, conn):
    """Accepted reply with ETA should text the customer and notify Eddie."""
    mock_sms.send_sms.return_value = "SM_TEST_SID"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"
    mock_classify.return_value = {
        "intent": "accepted",
        "time": "5pm",
        "reason": None,
        "condition": None,
        "raw_text": "Yes 5pm",
    }

    job = _create_test_job(db, conn)
    db.update_job(conn, job["id"], status="contacting_contractor", current_contractor="Jose")
    job = db.get_job(conn, job["id"])

    dispatch_module.process_contractor_reply(conn, job, "Jose", "Yes 5pm")

    customer_calls = [
        c for c in mock_sms.send_sms.call_args_list if c[0][0] == "+15551234567"
    ]
    assert len(customer_calls) == 1
    assert "Jose is confirmed for 5pm" in customer_calls[0][0][1]
    assert "Reply here if anything changes" in customer_calls[0][0][1]

    notif = mock_sms.send_eddie_notification.call_args[0][0]
    assert "Customer text sent" in notif
    assert "Jose is confirmed for 5pm" in notif


@patch("dispatch.classify_reply")
@patch("dispatch.sms")
def test_reply_accepted_without_eta_requests_eta(mock_sms, mock_classify, dispatch_module, db, conn):
    """Accepted reply without ETA should ask contractor for time and not text customer."""
    mock_sms.send_sms.return_value = "SM_TEST_SID"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"
    mock_classify.return_value = {
        "intent": "accepted",
        "time": None,
        "reason": None,
        "condition": None,
        "raw_text": "yes",
    }

    job = _create_test_job(db, conn)
    db.update_job(conn, job["id"], status="contacting_contractor", current_contractor="Jose")
    job = db.get_job(conn, job["id"])

    dispatch_module.process_contractor_reply(conn, job, "Jose", "yes")

    updated = db.get_job(conn, job["id"])
    assert updated["status"] == "accepted_waiting_eta"
    assert updated["current_contractor"] == "Jose"
    assert updated["next_action_at"] is None

    mock_sms.send_sms.assert_called_once()
    assert mock_sms.send_sms.call_args[0][0] == "+15550001111"
    assert "What time can you arrive" in mock_sms.send_sms.call_args[0][1]
    assert "+15551234567" not in [c[0][0] for c in mock_sms.send_sms.call_args_list]

    notif = mock_sms.send_eddie_notification.call_args[0][0]
    assert "accepted but did not provide an ETA" in notif
    assert "Customer has not been texted yet" in notif


@patch("dispatch.classify_reply")
@patch("dispatch.sms")
def test_reply_waiting_for_eta_accepts_time_only(mock_sms, mock_classify, dispatch_module, db, conn):
    """When waiting for ETA, a time-only reply should confirm and text customer."""
    mock_sms.send_sms.return_value = "SM_TEST_SID"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"
    mock_classify.return_value = {
        "intent": "unclear",
        "time": None,
        "reason": None,
        "condition": None,
        "raw_text": "5pm",
    }

    job = _create_test_job(db, conn)
    db.update_job(
        conn,
        job["id"],
        status="accepted_waiting_eta",
        current_contractor="Jose",
        contractor_response="yes",
    )
    job = db.get_job(conn, job["id"])

    dispatch_module.process_contractor_reply(conn, job, "Jose", "5pm")

    updated = db.get_job(conn, job["id"])
    assert updated["status"] == "contractor_confirmed"
    assert updated["confirmed_time"] == "5pm"
    customer_calls = [
        c for c in mock_sms.send_sms.call_args_list if c[0][0] == "+15551234567"
    ]
    assert len(customer_calls) == 1


# ---------------------------------------------------------------------------
# process_contractor_reply — accepted (emergency, notifies others)
# ---------------------------------------------------------------------------


@patch("dispatch.classify_reply")
@patch("dispatch.sms")
def test_reply_accepted_emergency_notifies_others(mock_sms, mock_classify, dispatch_module, db, conn):
    """Emergency accepted: notify other contacted contractors."""
    mock_sms.send_sms.return_value = "SM_TEST_SID"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"
    mock_classify.return_value = {
        "intent": "accepted",
        "time": None,
        "reason": None,
        "condition": None,
        "raw_text": "On my way",
    }

    job = _create_test_job(db, conn, priority="emergency")
    db.update_job(conn, job["id"], status="contacting_contractor", current_contractor="Jose")
    job = db.get_job(conn, job["id"])

    # Log outbound messages to simulate emergency dispatch to all 3
    db.log_message(conn, job["id"], "outbound", "Job text", contractor_name="Jose")
    db.log_message(conn, job["id"], "outbound", "Job text", contractor_name="Mario")
    db.log_message(conn, job["id"], "outbound", "Job text", contractor_name="Raul")

    dispatch_module.process_contractor_reply(conn, job, "Jose", "On my way")

    # Should notify Mario and Raul that Jose took the job
    sms_calls = mock_sms.send_sms.call_args_list
    job_taken_calls = [c for c in sms_calls if "has been taken" in c[0][1]]
    phones_notified = [c[0][0] for c in job_taken_calls]
    assert "+15550002222" in phones_notified  # Mario
    assert "+15550003333" in phones_notified  # Raul
    assert "+15550001111" not in phones_notified  # NOT Jose
    # Verify the message does NOT reveal who took it
    assert "Jose" not in job_taken_calls[0][0][1]


# ---------------------------------------------------------------------------
# process_contractor_reply — declined
# ---------------------------------------------------------------------------


@patch("dispatch.classify_reply")
@patch("dispatch.sms")
def test_reply_declined_escalates(mock_sms, mock_classify, dispatch_module, db, conn):
    """Declined reply should escalate to next contractor."""
    mock_sms.send_sms.return_value = "SM_TEST_SID"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"
    mock_classify.return_value = {
        "intent": "declined",
        "time": None,
        "reason": "too busy",
        "condition": None,
        "raw_text": "Can't make it",
    }

    job = _create_test_job(db, conn)
    db.update_job(conn, job["id"], status="contacting_contractor", current_contractor="Jose")
    job = db.get_job(conn, job["id"])

    dispatch_module.process_contractor_reply(conn, job, "Jose", "Can't make it")

    updated = db.get_job(conn, job["id"])
    assert updated["current_contractor"] == "Mario"
    assert updated["status"] == "contacting_contractor"


# ---------------------------------------------------------------------------
# process_contractor_reply — conditional
# ---------------------------------------------------------------------------


@patch("dispatch.classify_reply")
@patch("dispatch.sms")
def test_reply_conditional(mock_sms, mock_classify, dispatch_module, db, conn):
    """Conditional reply: update status and notify Eddie."""
    mock_sms.send_sms.return_value = "SM_TEST_SID"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"
    mock_classify.return_value = {
        "intent": "conditional",
        "time": None,
        "reason": None,
        "condition": "if parts are available",
        "raw_text": "I can do it if parts are available",
    }

    job = _create_test_job(db, conn)
    db.update_job(conn, job["id"], status="contacting_contractor", current_contractor="Jose")
    job = db.get_job(conn, job["id"])

    dispatch_module.process_contractor_reply(conn, job, "Jose", "I can do it if parts are available")

    updated = db.get_job(conn, job["id"])
    assert updated["status"] == "conditional_pending"

    notif = mock_sms.send_eddie_notification.call_args[0][0]
    assert "condition" in notif
    assert "OK to confirm" in notif


# ---------------------------------------------------------------------------
# process_contractor_reply — unclear
# ---------------------------------------------------------------------------


@patch("dispatch.classify_reply")
@patch("dispatch.sms")
def test_reply_unclear(mock_sms, mock_classify, dispatch_module, db, conn):
    """Unclear reply: notify Eddie, don't change status."""
    mock_sms.send_sms.return_value = "SM_TEST_SID"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"
    mock_classify.return_value = {
        "intent": "unclear",
        "time": None,
        "reason": None,
        "condition": None,
        "raw_text": "hmm let me think",
    }

    job = _create_test_job(db, conn)
    db.update_job(conn, job["id"], status="contacting_contractor", current_contractor="Jose")
    job = db.get_job(conn, job["id"])

    dispatch_module.process_contractor_reply(conn, job, "Jose", "hmm let me think")

    # Status should NOT change
    updated = db.get_job(conn, job["id"])
    assert updated["status"] == "contacting_contractor"

    notif = mock_sms.send_eddie_notification.call_args[0][0]
    assert "unclear" in notif


# ---------------------------------------------------------------------------
# escalation chain: Jose → Mario → Raul → no_contractor_available
# ---------------------------------------------------------------------------


@patch("dispatch.sms")
def test_escalation_chain_full(mock_sms, dispatch_module, db, conn):
    """Test full escalation: Jose -> Mario -> Raul -> no_contractor_available."""
    mock_sms.send_sms.return_value = "SM_TEST_SID"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"

    job = _create_test_job(db, conn)
    db.update_job(conn, job["id"], status="contacting_contractor", current_contractor="Jose")
    job = db.get_job(conn, job["id"])

    # Escalate from Jose -> Mario
    dispatch_module.escalate_to_next(conn, job)
    job = db.get_job(conn, job["id"])
    assert job["current_contractor"] == "Mario"
    assert job["status"] == "contacting_contractor"

    # Escalate from Mario -> Raul
    dispatch_module.escalate_to_next(conn, job)
    job = db.get_job(conn, job["id"])
    assert job["current_contractor"] == "Raul"
    assert job["status"] == "contacting_contractor"

    # Escalate from Raul -> none
    dispatch_module.escalate_to_next(conn, job)
    job = db.get_job(conn, job["id"])
    assert job["status"] == "no_contractor_available"

    mock_sms.send_eddie_notification.assert_called()
    assert "No contractors available" in mock_sms.send_eddie_notification.call_args[0][0]


# ---------------------------------------------------------------------------
# follow-up progression: attempt 1 → 2 → 3 → escalate
# ---------------------------------------------------------------------------


@patch("dispatch.sms")
def test_follow_up_progression(mock_sms, dispatch_module, db, conn):
    """Follow-up: attempt 1->2 (follow_up_1), 2->3 (follow_up_2), 3->escalate."""
    mock_sms.send_sms.return_value = "SM_TEST_SID"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"

    job = _create_test_job(db, conn)
    db.update_job(
        conn, job["id"],
        status="awaiting_reply",
        current_contractor="Jose",
        attempt_count=1,
    )
    job = db.get_job(conn, job["id"])

    # First follow-up: attempt 1 -> 2, status -> follow_up_1
    dispatch_module.process_follow_up(conn, job)
    job = db.get_job(conn, job["id"])
    assert job["attempt_count"] == 2
    assert job["status"] == "follow_up_1"

    # Second follow-up: attempt 2 -> 3, status -> follow_up_2
    dispatch_module.process_follow_up(conn, job)
    job = db.get_job(conn, job["id"])
    assert job["attempt_count"] == 3
    assert job["status"] == "follow_up_2"

    # Third follow-up: attempt 3 = MAX, should escalate to Mario
    dispatch_module.process_follow_up(conn, job)
    job = db.get_job(conn, job["id"])
    assert job["current_contractor"] == "Mario"
    assert job["attempt_count"] == 1


# ---------------------------------------------------------------------------
# upgrade_to_emergency
# ---------------------------------------------------------------------------


@patch("dispatch.sms")
def test_upgrade_to_emergency(mock_sms, dispatch_module, db, conn):
    """Upgrade contacts contractors not yet reached."""
    mock_sms.send_sms.return_value = "SM_TEST_SID"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"

    job = _create_test_job(db, conn)
    db.update_job(conn, job["id"], status="contacting_contractor", current_contractor="Jose")

    # Log that Jose was already contacted
    db.log_message(conn, job["id"], "outbound", "Job text", contractor_name="Jose")

    job = db.get_job(conn, job["id"])
    dispatch_module.upgrade_to_emergency(conn, job)

    # Should update priority
    updated = db.get_job(conn, job["id"])
    assert updated["priority"] == "emergency"

    # Should send to Mario and Raul (not Jose)
    sms_calls = mock_sms.send_sms.call_args_list
    phones_called = [c[0][0] for c in sms_calls]
    assert "+15550002222" in phones_called  # Mario
    assert "+15550003333" in phones_called  # Raul
    assert "+15550001111" not in phones_called  # NOT Jose


@patch("dispatch.sms")
def test_upgrade_to_emergency_all_already_contacted(mock_sms, dispatch_module, db, conn):
    """Upgrade when all contractors already contacted: notify Eddie."""
    mock_sms.send_sms.return_value = "SM_TEST_SID"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"

    job = _create_test_job(db, conn)
    db.update_job(conn, job["id"], status="contacting_contractor", current_contractor="Jose")

    # Log that all were already contacted
    for name in ("Jose", "Mario", "Raul"):
        db.log_message(conn, job["id"], "outbound", "Job text", contractor_name=name)

    job = db.get_job(conn, job["id"])
    dispatch_module.upgrade_to_emergency(conn, job)

    mock_sms.send_eddie_notification.assert_called_once_with(
        "Already contacting all available contractors"
    )


# ---------------------------------------------------------------------------
# process_eddie_command
# ---------------------------------------------------------------------------


@patch("dispatch.sms")
def test_eddie_command_ok(mock_sms, dispatch_module, db, conn):
    """OK command: confirm conditional job."""
    mock_sms.send_sms.return_value = "SM_TEST_SID"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"

    job = _create_test_job(db, conn)
    db.update_job(
        conn, job["id"],
        status="conditional_pending",
        current_contractor="Jose",
        contractor_response="I can if parts available",
    )
    job = db.get_job(conn, job["id"])

    dispatch_module.process_eddie_command(conn, job, "OK")

    updated = db.get_job(conn, job["id"])
    assert updated["status"] == "contractor_confirmed"

    notif = mock_sms.send_eddie_notification.call_args[0][0]
    assert "CONFIRMED" in notif


@patch("dispatch.sms")
def test_eddie_command_next(mock_sms, dispatch_module, db, conn):
    """NEXT command: escalate to next contractor."""
    mock_sms.send_sms.return_value = "SM_TEST_SID"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"

    job = _create_test_job(db, conn)
    db.update_job(
        conn, job["id"],
        status="conditional_pending",
        current_contractor="Jose",
    )
    job = db.get_job(conn, job["id"])

    dispatch_module.process_eddie_command(conn, job, "NEXT")

    updated = db.get_job(conn, job["id"])
    assert updated["current_contractor"] == "Mario"


@patch("dispatch.sms")
def test_eddie_command_urgent(mock_sms, dispatch_module, db, conn):
    """URGENT command: upgrade to emergency."""
    mock_sms.send_sms.return_value = "SM_TEST_SID"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"

    job = _create_test_job(db, conn)
    db.update_job(conn, job["id"], status="contacting_contractor", current_contractor="Jose")
    db.log_message(conn, job["id"], "outbound", "Job text", contractor_name="Jose")

    job = db.get_job(conn, job["id"])
    dispatch_module.process_eddie_command(conn, job, "URGENT")

    updated = db.get_job(conn, job["id"])
    assert updated["priority"] == "emergency"


@patch("dispatch.sms")
def test_eddie_command_cancel(mock_sms, dispatch_module, db, conn):
    """CANCEL command: set status to cancelled."""
    mock_sms.send_sms.return_value = "SM_TEST_SID"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"

    job = _create_test_job(db, conn)
    db.update_job(conn, job["id"], status="contacting_contractor", current_contractor="Jose")
    job = db.get_job(conn, job["id"])

    dispatch_module.process_eddie_command(conn, job, "CANCEL")

    updated = db.get_job(conn, job["id"])
    assert updated["status"] == "cancelled"

    mock_sms.send_eddie_notification.assert_called_once()
    assert "cancelled" in mock_sms.send_eddie_notification.call_args[0][0]


# ---------------------------------------------------------------------------
# Polling loop — processes overdue jobs
# ---------------------------------------------------------------------------


@patch("dispatch.sms")
def test_polling_processes_overdue_jobs(mock_sms, dispatch_module, db, conn):
    """Polling loop should process jobs with past next_action_at."""
    mock_sms.send_sms.return_value = "SM_TEST_SID"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"

    job = _create_test_job(db, conn)
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    db.update_job(
        conn, job["id"],
        status="awaiting_reply",
        current_contractor="Jose",
        attempt_count=1,
        next_action_at=past,
    )

    # Directly call the internal logic instead of the async loop
    jobs = db.get_jobs_needing_action(conn)
    assert len(jobs) == 1

    for j in jobs:
        dispatch_module.process_follow_up(conn, j)

    updated = db.get_job(conn, job["id"])
    assert updated["attempt_count"] == 2
    assert updated["status"] == "follow_up_1"


# ---------------------------------------------------------------------------
# Polling loop — expired confirmed jobs auto-complete
# ---------------------------------------------------------------------------


@patch("dispatch.sms")
def test_expired_confirmed_jobs_auto_complete(mock_sms, dispatch_module, db, conn):
    """Expired confirmed jobs should be auto-completed."""
    job = _create_test_job(db, conn)
    db.update_job(conn, job["id"], status="contractor_confirmed", current_contractor="Jose")

    # Manually set updated_at to >24 hours ago
    old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (old_time, job["id"]))
    conn.commit()

    expired = db.get_expired_confirmed_jobs(conn, 24)
    assert len(expired) == 1

    for j in expired:
        db.update_job(conn, j["id"], status="completed")

    updated = db.get_job(conn, job["id"])
    assert updated["status"] == "completed"


# ---------------------------------------------------------------------------
# Polling loop — heartbeat alerts
# ---------------------------------------------------------------------------


def test_heartbeat_alert_disabled_when_threshold_zero(dispatch_module):
    """HEARTBEAT_HOURS <= 0 disables no-new-jobs alerts."""
    dispatch_module.config.HEARTBEAT_HOURS = 0
    dispatch_module.config.HEARTBEAT_ALERT_INTERVAL_HOURS = 24
    dispatch_module._last_heartbeat_alert_at = None

    now = datetime(2026, 4, 15, 15, 0, tzinfo=timezone.utc)
    last_created = "2026-04-14 00:00:00"

    assert dispatch_module._should_send_heartbeat_alert(now, last_created) is False


def test_heartbeat_alert_throttles_repeated_stale_checks(dispatch_module):
    """A stale last job should alert once, then wait for the throttle window."""
    dispatch_module.config.HEARTBEAT_HOURS = 12
    dispatch_module.config.HEARTBEAT_ALERT_INTERVAL_HOURS = 24
    dispatch_module._last_heartbeat_alert_at = None

    now = datetime(2026, 4, 15, 15, 0, tzinfo=timezone.utc)
    last_created = "2026-04-14 00:00:00"

    assert dispatch_module._should_send_heartbeat_alert(now, last_created) is True
    assert dispatch_module._should_send_heartbeat_alert(
        now + timedelta(minutes=1), last_created
    ) is False
    assert dispatch_module._should_send_heartbeat_alert(
        now + timedelta(hours=24, minutes=1), last_created
    ) is True


# ---------------------------------------------------------------------------
# Polling loop — async integration test
# ---------------------------------------------------------------------------


@patch("dispatch.sms")
def test_polling_loop_runs_one_iteration(mock_sms, dispatch_module, db, conn):
    """Test that the polling loop processes one iteration correctly."""
    import asyncio

    mock_sms.send_sms.return_value = "SM_TEST_SID"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"
    mock_sms.send_error_alert = MagicMock()

    job = _create_test_job(db, conn)
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    db.update_job(
        conn, job["id"],
        status="awaiting_reply",
        current_contractor="Jose",
        attempt_count=1,
        next_action_at=past,
    )

    # Patch sleep to break after one iteration
    # First call is the startup delay, second is the loop interval
    call_count = 0
    async def mock_sleep(seconds):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise asyncio.CancelledError()

    loop = asyncio.new_event_loop()
    with patch("asyncio.sleep", side_effect=mock_sleep):
        with pytest.raises(asyncio.CancelledError):
            loop.run_until_complete(dispatch_module.run_polling_loop("test.db"))
    loop.close()

    updated = db.get_job(conn, job["id"])
    assert updated["attempt_count"] == 2


# ---------------------------------------------------------------------------
# Edge case: escalation goes to next contractor in priority order
# ---------------------------------------------------------------------------


@patch("dispatch.sms")
def test_escalation_goes_to_mario_after_jose(mock_sms, dispatch_module, db, conn):
    """Escalation from Jose should go to Mario (next priority)."""
    mock_sms.send_sms.return_value = "SM_TEST_SID"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"

    job = _create_test_job(db, conn)
    db.update_job(conn, job["id"], status="contacting_contractor", current_contractor="Jose")
    job = db.get_job(conn, job["id"])

    dispatch_module.escalate_to_next(conn, job)

    updated = db.get_job(conn, job["id"])
    assert updated["current_contractor"] == "Mario"


# ---------------------------------------------------------------------------
# Message logging
# ---------------------------------------------------------------------------


@patch("dispatch.sms")
def test_outbound_message_logged(mock_sms, dispatch_module, db, conn):
    """start_dispatch should log outbound messages."""
    mock_sms.send_sms.return_value = "SM_TEST_SID"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"

    job = _create_test_job(db, conn)
    dispatch_module.start_dispatch(conn, job["id"])

    messages = conn.execute(
        "SELECT * FROM messages WHERE job_id = ? AND contractor_name != 'Eddie'", (job["id"],)
    ).fetchall()
    assert len(messages) == 1
    assert messages[0]["direction"] == "outbound"
    assert messages[0]["contractor_name"] == "Jose"


@patch("dispatch.classify_reply")
@patch("dispatch.sms")
def test_inbound_message_logged(mock_sms, mock_classify, dispatch_module, db, conn):
    """process_contractor_reply should log inbound messages with intent."""
    mock_sms.send_sms.return_value = "SM_TEST_SID"
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"
    mock_classify.return_value = {
        "intent": "accepted",
        "time": None,
        "reason": None,
        "condition": None,
        "raw_text": "yes",
    }

    job = _create_test_job(db, conn)
    db.update_job(conn, job["id"], status="contacting_contractor", current_contractor="Jose")
    job = db.get_job(conn, job["id"])

    dispatch_module.process_contractor_reply(conn, job, "Jose", "yes")

    messages = conn.execute(
        "SELECT * FROM messages WHERE job_id = ? AND direction = 'inbound'",
        (job["id"],),
    ).fetchall()
    assert len(messages) == 1
    assert messages[0]["parsed_intent"] == "accepted"
    assert messages[0]["contractor_name"] == "Jose"


@patch("dispatch.sms")
def test_customer_reply_relayed_to_eddie(mock_sms, dispatch_module, db, conn):
    """Customer replies should be logged and relayed with exact text."""
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"

    job = _create_test_job(db, conn)
    db.update_job(
        conn,
        job["id"],
        status="contractor_confirmed",
        current_contractor="Jose",
        confirmed_time="5pm",
    )
    job = db.get_job(conn, job["id"])

    dispatch_module.process_customer_reply(
        conn,
        job,
        "+15551234567",
        "Can he come earlier?",
        twilio_message_sid="SM_CUSTOMER_1",
    )

    notif = mock_sms.send_eddie_notification.call_args[0][0]
    assert "Customer reply" in notif
    assert "Can he come earlier?" in notif
    assert "Job #" in notif

    messages = conn.execute(
        "SELECT * FROM messages WHERE job_id = ? AND contractor_name = 'Customer'",
        (job["id"],),
    ).fetchall()
    assert len(messages) == 1
    assert messages[0]["direction"] == "inbound"
    assert messages[0]["body"] == "Can he come earlier?"


@patch("dispatch.sms")
def test_customer_reply_duplicate_sid_ignored(mock_sms, dispatch_module, db, conn):
    """Duplicate Twilio retries should not relay the same customer reply twice."""
    mock_sms.send_eddie_notification.return_value = "SM_NOTIFY"

    job = _create_test_job(db, conn)
    db.update_job(conn, job["id"], status="contractor_confirmed")
    job = db.get_job(conn, job["id"])

    dispatch_module.process_customer_reply(
        conn, job, "+15551234567", "OK", twilio_message_sid="SM_DUP_CUSTOMER"
    )
    dispatch_module.process_customer_reply(
        conn, job, "+15551234567", "OK again", twilio_message_sid="SM_DUP_CUSTOMER"
    )

    mock_sms.send_eddie_notification.assert_called_once()
