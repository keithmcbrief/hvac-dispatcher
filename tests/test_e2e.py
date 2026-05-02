"""End-to-end integration tests for Eddie's HVAC Contractor Dispatch Agent.

These tests exercise the full webhook -> dispatch -> notification flow using
FastAPI's TestClient with a real SQLite database. Only external services
(Twilio SMS, OpenAI API) are mocked.
"""

import importlib
import json
from unittest.mock import patch, MagicMock, call

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _setup_env(monkeypatch, tmp_path):
    """Set up a fresh temp database and config for every test."""
    db_file = str(tmp_path / "e2e_test.db")
    monkeypatch.setenv("DB_PATH", db_file)

    env_file = tmp_path / ".env"
    env_file.write_text("")
    monkeypatch.chdir(tmp_path)

    monkeypatch.setenv("JOSE_PHONE", "+15550001111")
    monkeypatch.setenv("MARIO_PHONE", "+15550002222")
    monkeypatch.setenv("RAUL_PHONE", "+15550003333")
    monkeypatch.setenv("JOSE_ACTIVE", "false")
    monkeypatch.setenv("MARIO_ACTIVE", "true")
    monkeypatch.setenv("RAUL_ACTIVE", "true")
    monkeypatch.setenv("EDDIE_PHONE", "+15550009999")
    monkeypatch.setenv("BUILDER_PHONE", "+15550008888")
    monkeypatch.setenv("RETELL_API_KEY", "test-retell-key")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-twilio-token")
    monkeypatch.setenv("DASHBOARD_SLUG", "test-dash")

    import config
    importlib.reload(config)

    import db
    importlib.reload(db)
    db.init_db()

    import dispatch
    importlib.reload(dispatch)

    import main
    importlib.reload(main)

    yield db


@pytest.fixture
def db_mod(_setup_env):
    return _setup_env


@pytest.fixture
def conn(db_mod):
    c = db_mod.get_connection()
    yield c
    c.close()


@pytest.fixture
def dispatch_mod():
    import dispatch
    return dispatch


@pytest.fixture
def client():
    import main
    return TestClient(main.app, raise_server_exceptions=False)


def _retell_payload(
    customer_name="Jane Customer",
    phone="+15551234567",
    address="100 Oak Ave",
    service_type="AC Repair",
    issue_description="Unit not cooling",
    priority="normal",
    call_id="retell-e2e-001",
):
    return {
        "call_id": call_id,
        "customer_name": customer_name,
        "phone": phone,
        "address": address,
        "service_type": service_type,
        "issue_description": issue_description,
        "priority": priority,
    }


def _post_retell(client, payload):
    body = json.dumps(payload).encode()
    return client.post(
        "/webhook/retell",
        content=body,
        headers={"x-retell-signature": "valid"},
    )


def _post_twilio(client, from_number, body_text, message_sid="SM-auto"):
    return client.post(
        "/webhook/twilio",
        data={"From": from_number, "Body": body_text, "MessageSid": message_sid},
        headers={"x-twilio-signature": "valid"},
    )


# ---------------------------------------------------------------------------
# Test 1: Happy path -- Retell webhook -> contractor accepts
# ---------------------------------------------------------------------------

@patch("dispatch.classify_reply")
@patch("main.sms.validate_twilio_signature", return_value=True)
@patch("main.sms.validate_retell_signature", return_value=True)
@patch("dispatch.sms.send_eddie_notification")
@patch("dispatch.sms.send_sms")
def test_happy_path_contractor_accepts(
    mock_send_sms, mock_eddie_notify,
    mock_retell_sig, mock_twilio_sig, mock_classify,
    client, db_mod, conn, dispatch_mod,
):
    """Full flow: Retell webhook creates job -> SMS to Mario -> Mario accepts -> Eddie notified."""
    mock_send_sms.return_value = "SM_FAKE_SID"
    mock_eddie_notify.return_value = "SM_EDDIE_SID"

    # Step 1: POST /webhook/retell with a job
    payload = _retell_payload()
    resp = _post_retell(client, payload)
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    # Step 2: Verify SMS was sent to Mario (first active priority)
    assert mock_send_sms.called
    mario_calls = [c for c in mock_send_sms.call_args_list if c[0][0] == "+15550002222"]
    assert len(mario_calls) >= 1, "Expected SMS to Mario"
    assert f"#{job_id}" in mario_calls[0][0][1]
    assert "Customer: Jane Customer (+15551234567)" in mario_calls[0][0][1]
    assert "Contact the customer directly" in mario_calls[0][0][1]

    # Step 3: Mario replies "yeah tuesday 2pm"
    mock_classify.return_value = {
        "intent": "accepted",
        "time": "Tuesday 2pm",
        "reason": None,
        "condition": None,
        "raw_text": "yeah tuesday 2pm",
    }

    resp = _post_twilio(client, "+15550002222", "yeah tuesday 2pm", "SM-mario-001")
    assert resp.status_code == 200

    # Step 4: Verify classifier was called, job status is contractor_confirmed
    mock_classify.assert_called()

    job = db_mod.get_job(conn, job_id)
    assert job["status"] == "contractor_confirmed"
    assert job["current_contractor"] == "Mario"
    assert job["confirmed_time"] == "Tuesday 2pm"

    # Step 5: Verify Eddie got a notification SMS
    mock_eddie_notify.assert_called()
    eddie_msg = mock_eddie_notify.call_args[0][0]
    assert "Mario" in eddie_msg
    assert "confirmed" in eddie_msg.lower() or "✓" in eddie_msg


# ---------------------------------------------------------------------------
# Test 2: Escalation -- Mario doesn't reply, Raul accepts
# ---------------------------------------------------------------------------

@patch("dispatch.classify_reply")
@patch("main.sms.validate_twilio_signature", return_value=True)
@patch("main.sms.validate_retell_signature", return_value=True)
@patch("dispatch.sms.send_error_alert")
@patch("dispatch.sms.send_eddie_notification")
@patch("dispatch.sms.send_sms")
def test_escalation_mario_no_reply_raul_accepts(
    mock_send_sms, mock_eddie_notify, mock_error_alert,
    mock_retell_sig, mock_twilio_sig, mock_classify,
    client, db_mod, conn, dispatch_mod,
):
    """Mario doesn't reply -> follow_up_1 -> follow_up_2 -> escalate to Raul -> Raul accepts."""
    dispatch_mod.config.JOB_POLLING_ENABLED = True
    mock_send_sms.return_value = "SM_FAKE_SID"
    mock_eddie_notify.return_value = "SM_EDDIE_SID"

    # Step 1: POST /webhook/retell with a job
    payload = _retell_payload(call_id="retell-escalation-001")
    resp = _post_retell(client, payload)
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    # Verify SMS sent to Mario
    mario_calls = [c for c in mock_send_sms.call_args_list if c[0][0] == "+15550002222"]
    assert len(mario_calls) >= 1, "Expected initial SMS to Mario"

    # Step 3: Trigger follow-up (simulate time passing) -> follow_up_1
    job = db_mod.get_job(conn, job_id)
    assert job["status"] == "contacting_contractor"
    assert job["attempt_count"] == 1

    dispatch_mod.process_follow_up(conn, job)

    job = db_mod.get_job(conn, job_id)
    assert job["status"] == "follow_up_1"
    assert job["attempt_count"] == 2

    # Step 4: Verify follow-up SMS sent to Mario
    mario_calls_after = [c for c in mock_send_sms.call_args_list if c[0][0] == "+15550002222"]
    assert len(mario_calls_after) >= 2, "Expected follow-up SMS to Mario"
    assert "Following up" in mario_calls_after[-1][0][1]

    # Step 5: Trigger another follow-up -> follow_up_2
    dispatch_mod.process_follow_up(conn, job)

    job = db_mod.get_job(conn, job_id)
    assert job["status"] == "follow_up_2"
    assert job["attempt_count"] == 3

    # Step 6: Max attempts reached, escalate to Raul
    dispatch_mod.process_follow_up(conn, job)

    job = db_mod.get_job(conn, job_id)
    assert job["current_contractor"] == "Raul"
    assert job["status"] == "contacting_contractor"

    # Verify SMS sent to Raul
    raul_calls = [c for c in mock_send_sms.call_args_list if c[0][0] == "+15550003333"]
    assert len(raul_calls) >= 1, "Expected SMS to Raul"
    assert f"#{job_id}" in raul_calls[0][0][1]

    # Step 7: Raul replies "on my way"
    mock_classify.return_value = {
        "intent": "accepted",
        "time": None,
        "reason": None,
        "condition": None,
        "raw_text": "on my way",
    }

    resp = _post_twilio(client, "+15550003333", "on my way", "SM-raul-001")
    assert resp.status_code == 200

    # Step 8: Verify job confirmed with Raul
    job = db_mod.get_job(conn, job_id)
    assert job["status"] == "contractor_confirmed"
    assert job["current_contractor"] == "Raul"

    mock_eddie_notify.assert_called()
    eddie_msg = mock_eddie_notify.call_args[0][0]
    assert "Raul" in eddie_msg


# ---------------------------------------------------------------------------
# Test 3: Emergency -- all contractors texted, first wins
# ---------------------------------------------------------------------------

@patch("dispatch.classify_reply")
@patch("main.sms.validate_twilio_signature", return_value=True)
@patch("main.sms.validate_retell_signature", return_value=True)
@patch("dispatch.sms.send_eddie_notification")
@patch("dispatch.sms.send_sms")
def test_emergency_all_contacted_first_wins(
    mock_send_sms, mock_eddie_notify,
    mock_retell_sig, mock_twilio_sig, mock_classify,
    client, db_mod, conn, dispatch_mod,
):
    """Emergency: all active contractors texted, Mario replies first, others get 'Job taken'."""
    mock_send_sms.return_value = "SM_FAKE_SID"
    mock_eddie_notify.return_value = "SM_EDDIE_SID"

    # Step 1: POST /webhook/retell with priority=emergency
    payload = _retell_payload(priority="emergency", call_id="retell-emergency-001")
    resp = _post_retell(client, payload)
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    # Step 2: Verify SMS sent to all active contractors
    phones_sent = [c[0][0] for c in mock_send_sms.call_args_list]
    assert "+15550001111" not in phones_sent, "Jose should be paused"
    assert "+15550002222" in phones_sent, "Expected SMS to Mario"
    assert "+15550003333" in phones_sent, "Expected SMS to Raul"

    # Reset to track subsequent calls
    mock_send_sms.reset_mock()
    mock_send_sms.return_value = "SM_FAKE_SID"

    # Step 3: Mario replies "yes"
    mock_classify.return_value = {
        "intent": "accepted",
        "time": None,
        "reason": None,
        "condition": None,
        "raw_text": "yes",
    }

    resp = _post_twilio(client, "+15550002222", "yes", "SM-mario-emerg-001")
    assert resp.status_code == 200

    # Step 4: Verify the job is confirmed without an ETA/customer text in technician handoff mode
    job = db_mod.get_job(conn, job_id)
    assert job["status"] == "contractor_confirmed"
    assert job["current_contractor"] == "Mario"
    assert job["confirmed_time"] == "not specified"
    eta_requests = [c for c in mock_send_sms.call_args_list if "Reply with ETA only" in c[0][1]]
    assert len(eta_requests) == 0

    mock_eddie_notify.assert_called()
    eddie_msg = mock_eddie_notify.call_args[0][0]
    assert "Mario" in eddie_msg
    assert "Customer was NOT texted automatically" in eddie_msg

    # Step 5: Verify customer was not texted and Raul got job-taken notice
    customer_calls = [c for c in mock_send_sms.call_args_list if c[0][0] == "+15551234567"]
    assert len(customer_calls) == 0

    taken_calls = [c for c in mock_send_sms.call_args_list if "has been taken" in c[0][1]]
    taken_phones = [c[0][0] for c in taken_calls]
    assert "+15550002222" not in taken_phones, "Mario accepted the job"
    assert "+15550003333" in taken_phones, "Expected job-taken notice to Raul"


# ---------------------------------------------------------------------------
# Test 4: Eddie intervention -- conditional reply, Eddie confirms
# ---------------------------------------------------------------------------

@patch("dispatch.classify_reply")
@patch("main.sms.validate_twilio_signature", return_value=True)
@patch("main.sms.validate_retell_signature", return_value=True)
@patch("dispatch.sms.send_eddie_notification")
@patch("dispatch.sms.send_sms")
def test_eddie_intervention_conditional_then_ok(
    mock_send_sms, mock_eddie_notify,
    mock_retell_sig, mock_twilio_sig, mock_classify,
    client, db_mod, conn, dispatch_mod,
):
    """Mario replies with condition -> Eddie gets notified -> Eddie replies OK -> job confirmed."""
    mock_send_sms.return_value = "SM_FAKE_SID"
    mock_eddie_notify.return_value = "SM_EDDIE_SID"

    # Step 1: POST /webhook/retell with a job
    payload = _retell_payload(call_id="retell-conditional-001")
    resp = _post_retell(client, payload)
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    # Step 2: Mario replies "only if before 5pm"
    mock_classify.return_value = {
        "intent": "conditional",
        "time": None,
        "reason": None,
        "condition": "only if before 5pm",
        "raw_text": "only if before 5pm",
    }

    resp = _post_twilio(client, "+15550002222", "only if before 5pm", "SM-mario-cond-001")
    assert resp.status_code == 200

    # Step 3: Verify Eddie gets conditional notification
    job = db_mod.get_job(conn, job_id)
    assert job["status"] == "conditional_pending"

    mock_eddie_notify.assert_called()
    eddie_msg = mock_eddie_notify.call_args[0][0]
    assert "condition" in eddie_msg.lower() or "conditional" in eddie_msg.lower() or "only if before 5pm" in eddie_msg

    # Step 4: Eddie replies "OK"
    mock_eddie_notify.reset_mock()
    mock_eddie_notify.return_value = "SM_EDDIE_SID"

    resp = _post_twilio(client, "+15550009999", "OK", "SM-eddie-ok-001")
    assert resp.status_code == 200

    # Step 5: Verify job confirmed
    job = db_mod.get_job(conn, job_id)
    assert job["status"] == "contractor_confirmed"
    assert job["current_contractor"] == "Mario"

    # Eddie should have gotten a confirmation notification
    mock_eddie_notify.assert_called()
    confirm_msg = mock_eddie_notify.call_args[0][0]
    assert "confirmed" in confirm_msg.lower() or "✓" in confirm_msg
