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
    """Full flow: Retell webhook creates job -> SMS to Jose -> Jose accepts -> Eddie notified."""
    mock_send_sms.return_value = "SM_FAKE_SID"
    mock_eddie_notify.return_value = "SM_EDDIE_SID"

    # Step 1: POST /webhook/retell with a job
    payload = _retell_payload()
    resp = _post_retell(client, payload)
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    # Step 2: Verify SMS was sent to Jose (first in priority)
    assert mock_send_sms.called
    jose_calls = [c for c in mock_send_sms.call_args_list if c[0][0] == "+15550001111"]
    assert len(jose_calls) >= 1, "Expected SMS to Jose"
    assert f"#{job_id}" in jose_calls[0][0][1]
    assert "Reply YES + ETA" in jose_calls[0][0][1]
    assert "or NO" in jose_calls[0][0][1]

    # Step 3: Jose replies "yeah tuesday 2pm"
    mock_classify.return_value = {
        "intent": "accepted",
        "time": "Tuesday 2pm",
        "reason": None,
        "condition": None,
        "raw_text": "yeah tuesday 2pm",
    }

    resp = _post_twilio(client, "+15550001111", "yeah tuesday 2pm", "SM-jose-001")
    assert resp.status_code == 200

    # Step 4: Verify classifier was called, job status is contractor_confirmed
    mock_classify.assert_called()

    job = db_mod.get_job(conn, job_id)
    assert job["status"] == "contractor_confirmed"
    assert job["current_contractor"] == "Jose"
    assert job["confirmed_time"] == "Tuesday 2pm"

    # Step 5: Verify Eddie got a notification SMS
    mock_eddie_notify.assert_called()
    eddie_msg = mock_eddie_notify.call_args[0][0]
    assert "Jose" in eddie_msg
    assert "confirmed" in eddie_msg.lower() or "✓" in eddie_msg


# ---------------------------------------------------------------------------
# Test 2: Escalation -- Jose doesn't reply, Mario accepts
# ---------------------------------------------------------------------------

@patch("dispatch.classify_reply")
@patch("main.sms.validate_twilio_signature", return_value=True)
@patch("main.sms.validate_retell_signature", return_value=True)
@patch("dispatch.sms.send_error_alert")
@patch("dispatch.sms.send_eddie_notification")
@patch("dispatch.sms.send_sms")
def test_escalation_jose_no_reply_mario_accepts(
    mock_send_sms, mock_eddie_notify, mock_error_alert,
    mock_retell_sig, mock_twilio_sig, mock_classify,
    client, db_mod, conn, dispatch_mod,
):
    """Jose doesn't reply -> follow_up_1 -> follow_up_2 -> escalate to Mario -> Mario accepts."""
    mock_send_sms.return_value = "SM_FAKE_SID"
    mock_eddie_notify.return_value = "SM_EDDIE_SID"

    # Step 1: POST /webhook/retell with a job
    payload = _retell_payload(call_id="retell-escalation-001")
    resp = _post_retell(client, payload)
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    # Verify SMS sent to Jose
    jose_calls = [c for c in mock_send_sms.call_args_list if c[0][0] == "+15550001111"]
    assert len(jose_calls) >= 1, "Expected initial SMS to Jose"

    # Step 3: Trigger follow-up (simulate time passing) -> follow_up_1
    job = db_mod.get_job(conn, job_id)
    assert job["status"] == "contacting_contractor"
    assert job["attempt_count"] == 1

    dispatch_mod.process_follow_up(conn, job)

    job = db_mod.get_job(conn, job_id)
    assert job["status"] == "follow_up_1"
    assert job["attempt_count"] == 2

    # Step 4: Verify follow-up SMS sent to Jose
    jose_calls_after = [c for c in mock_send_sms.call_args_list if c[0][0] == "+15550001111"]
    assert len(jose_calls_after) >= 2, "Expected follow-up SMS to Jose"
    assert "Following up" in jose_calls_after[-1][0][1]

    # Step 5: Trigger another follow-up -> follow_up_2
    dispatch_mod.process_follow_up(conn, job)

    job = db_mod.get_job(conn, job_id)
    assert job["status"] == "follow_up_2"
    assert job["attempt_count"] == 3

    # Step 6: Max attempts reached, escalate to Mario
    dispatch_mod.process_follow_up(conn, job)

    job = db_mod.get_job(conn, job_id)
    assert job["current_contractor"] == "Mario"
    assert job["status"] == "contacting_contractor"

    # Verify SMS sent to Mario
    mario_calls = [c for c in mock_send_sms.call_args_list if c[0][0] == "+15550002222"]
    assert len(mario_calls) >= 1, "Expected SMS to Mario"
    assert f"#{job_id}" in mario_calls[0][0][1]

    # Step 7: Mario replies "on my way"
    mock_classify.return_value = {
        "intent": "accepted",
        "time": None,
        "reason": None,
        "condition": None,
        "raw_text": "on my way",
    }

    resp = _post_twilio(client, "+15550002222", "on my way", "SM-mario-001")
    assert resp.status_code == 200

    # Step 8: Verify job confirmed with Mario
    job = db_mod.get_job(conn, job_id)
    assert job["status"] == "contractor_confirmed"
    assert job["current_contractor"] == "Mario"

    mock_eddie_notify.assert_called()
    eddie_msg = mock_eddie_notify.call_args[0][0]
    assert "Mario" in eddie_msg


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
    """Emergency: all 3 contractors texted, Jose replies first, others get 'Job taken'."""
    mock_send_sms.return_value = "SM_FAKE_SID"
    mock_eddie_notify.return_value = "SM_EDDIE_SID"

    # Step 1: POST /webhook/retell with priority=emergency
    payload = _retell_payload(priority="emergency", call_id="retell-emergency-001")
    resp = _post_retell(client, payload)
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    # Step 2: Verify SMS sent to all 3 contractors
    phones_sent = [c[0][0] for c in mock_send_sms.call_args_list]
    assert "+15550001111" in phones_sent, "Expected SMS to Jose"
    assert "+15550002222" in phones_sent, "Expected SMS to Mario"
    assert "+15550003333" in phones_sent, "Expected SMS to Raul"

    # Reset to track subsequent calls
    mock_send_sms.reset_mock()
    mock_send_sms.return_value = "SM_FAKE_SID"

    # Step 3: Jose replies "yes"
    mock_classify.return_value = {
        "intent": "accepted",
        "time": None,
        "reason": None,
        "condition": None,
        "raw_text": "yes",
    }

    resp = _post_twilio(client, "+15550001111", "yes", "SM-jose-emerg-001")
    assert resp.status_code == 200

    # Step 4: Verify job is waiting for Jose's ETA before customer notification
    job = db_mod.get_job(conn, job_id)
    assert job["status"] == "accepted_waiting_eta"
    assert job["current_contractor"] == "Jose"
    eta_requests = [c for c in mock_send_sms.call_args_list if "Reply with ETA only" in c[0][1]]
    assert len(eta_requests) == 1
    assert eta_requests[0][0][0] == "+15550001111"
    assert "3-4pm" in eta_requests[0][0][1]

    mock_eddie_notify.assert_called()
    eddie_msg = mock_eddie_notify.call_args[0][0]
    assert "Jose" in eddie_msg
    assert "did not provide an ETA" in eddie_msg

    # Step 5: Jose provides the exact ETA reply from production; now the job is confirmed
    mock_classify.return_value = {
        "intent": "unclear",
        "time": None,
        "reason": None,
        "condition": None,
        "raw_text": "Between 3 and 4",
    }

    resp = _post_twilio(client, "+15550001111", "Between 3 and 4", "SM-jose-emerg-002")
    assert resp.status_code == 200

    job = db_mod.get_job(conn, job_id)
    assert job["status"] == "contractor_confirmed"
    assert job["current_contractor"] == "Jose"
    assert job["confirmed_time"] == "Between 3 and 4"

    # Step 6: Verify customer was texted and Mario/Raul got job-taken notice
    customer_calls = [c for c in mock_send_sms.call_args_list if c[0][0] == "+15551234567"]
    assert len(customer_calls) == 1
    assert "Jose is confirmed for Between 3 and 4" in customer_calls[0][0][1]

    taken_calls = [c for c in mock_send_sms.call_args_list if "has been taken" in c[0][1]]
    taken_phones = [c[0][0] for c in taken_calls]
    assert "+15550002222" in taken_phones, "Expected job-taken notice to Mario"
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
    """Jose replies with condition -> Eddie gets notified -> Eddie replies OK -> job confirmed."""
    mock_send_sms.return_value = "SM_FAKE_SID"
    mock_eddie_notify.return_value = "SM_EDDIE_SID"

    # Step 1: POST /webhook/retell with a job
    payload = _retell_payload(call_id="retell-conditional-001")
    resp = _post_retell(client, payload)
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    # Step 2: Jose replies "only if before 5pm"
    mock_classify.return_value = {
        "intent": "conditional",
        "time": None,
        "reason": None,
        "condition": "only if before 5pm",
        "raw_text": "only if before 5pm",
    }

    resp = _post_twilio(client, "+15550001111", "only if before 5pm", "SM-jose-cond-001")
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
    assert job["current_contractor"] == "Jose"

    # Eddie should have gotten a confirmation notification
    mock_eddie_notify.assert_called()
    confirm_msg = mock_eddie_notify.call_args[0][0]
    assert "confirmed" in confirm_msg.lower() or "✓" in confirm_msg
