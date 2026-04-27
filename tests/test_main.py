"""Tests for the FastAPI main app routes."""

import importlib
import hashlib
import hmac
import json
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator


@pytest.fixture(autouse=True)
def _use_temp_db(monkeypatch, tmp_path):
    """Point DB_PATH to a temp file and set up config for every test."""
    db_file = str(tmp_path / "test.db")
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

    # Reload main after config/db are ready
    import main
    importlib.reload(main)

    yield db


@pytest.fixture
def client(_use_temp_db):
    import main
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def conn(_use_temp_db):
    c = _use_temp_db.get_connection()
    yield c
    c.close()


@pytest.fixture
def db(_use_temp_db):
    return _use_temp_db


def _make_retell_signature(body_bytes: bytes, api_key: str = "test-retell-key") -> str:
    return hmac.new(api_key.encode(), body_bytes, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["version"] == "1.0.0"


# ---------------------------------------------------------------------------
# /webhook/retell
# ---------------------------------------------------------------------------

class TestRetellWebhook:

    def test_invalid_signature_returns_403(self, client):
        body = json.dumps({"call_id": "abc123"}).encode()
        resp = client.post(
            "/webhook/retell",
            content=body,
            headers={"x-retell-signature": "badsig"},
        )
        assert resp.status_code == 403

    @patch("main.dispatch.start_dispatch")
    def test_valid_webhook_token_accepts_invalid_signature(self, mock_dispatch, client, monkeypatch):
        import main
        monkeypatch.setattr(main.config, "RETELL_WEBHOOK_TOKEN", "test-retell-token")

        payload = {
            "call_id": "retell-token-001",
            "customer_name": "Token Customer",
            "phone": "+15551234567",
            "address": "123 Main St",
            "service_type": "AC Repair",
            "issue_description": "Unit not cooling",
        }
        body = json.dumps(payload).encode()
        resp = client.post(
            "/webhook/retell?token=test-retell-token",
            content=body,
            headers={"x-retell-signature": "badsig"},
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        mock_dispatch.assert_called_once()

    @patch("main.dispatch.start_dispatch")
    def test_valid_webhook_path_token_accepts_invalid_signature(self, mock_dispatch, client, monkeypatch):
        import main
        monkeypatch.setattr(main.config, "RETELL_WEBHOOK_TOKEN", "test-retell-token")

        payload = {
            "call_id": "retell-path-token-001",
            "customer_name": "Token Customer",
            "phone": "+15551234567",
            "address": "123 Main St",
            "service_type": "AC Repair",
            "issue_description": "Unit not cooling",
        }
        body = json.dumps(payload).encode()
        resp = client.post(
            "/webhook/retell/test-retell-token",
            content=body,
            headers={"x-retell-signature": "badsig"},
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        mock_dispatch.assert_called_once()

    @patch("main.dispatch.start_dispatch")
    @patch("main.sms.validate_retell_signature", return_value=True)
    def test_creates_job_and_dispatches(self, mock_validate, mock_dispatch, client):
        payload = {
            "call_id": "retell-001",
            "customer_name": "John Doe",
            "phone": "+15551234567",
            "address": "123 Main St",
            "service_type": "AC Repair",
            "issue_description": "Unit not cooling",
            "priority": "normal",
        }
        body = json.dumps(payload).encode()
        resp = client.post(
            "/webhook/retell",
            content=body,
            headers={"x-retell-signature": "anything"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "job_id" in data
        mock_dispatch.assert_called_once()

    @patch("main.dispatch.start_dispatch")
    @patch("main.sms.validate_retell_signature", return_value=True)
    def test_dedup_same_call_id(self, mock_validate, mock_dispatch, client):
        payload = {
            "call_id": "retell-dedup",
            "customer_name": "Jane Doe",
            "phone": "+15559999999",
            "address": "456 Oak Ave",
            "service_type": "Heating",
            "issue_description": "Furnace broken",
        }
        body = json.dumps(payload).encode()
        headers = {"x-retell-signature": "anything"}

        resp1 = client.post("/webhook/retell", content=body, headers=headers)
        assert resp1.status_code == 200
        job_id_1 = resp1.json()["job_id"]

        # Second call with same call_id — dispatch should NOT be called again
        mock_dispatch.reset_mock()

        # Need to update the job status away from 'new' to simulate dispatch happened
        import db as db_mod
        c = db_mod.get_connection()
        db_mod.update_job(c, job_id_1, status="contacting_contractor")
        c.close()

        resp2 = client.post("/webhook/retell", content=body, headers=headers)
        assert resp2.status_code == 200
        job_id_2 = resp2.json()["job_id"]
        assert job_id_1 == job_id_2
        mock_dispatch.assert_not_called()

    @patch("main.dispatch.start_dispatch")
    @patch("main.sms.validate_retell_signature", return_value=True)
    def test_nested_retell_format(self, mock_validate, mock_dispatch, client):
        """Test Retell's nested call.analysis format."""
        payload = {
            "event": "call_ended",
            "call": {
                "call_id": "retell-nested-001",
                "analysis": {
                    "customer_name": "Nested Customer",
                    "phone": "+15551112222",
                    "address": "789 Elm St",
                    "service_type": "Plumbing",
                    "issue_description": "Pipe leak",
                    "priority": "emergency",
                },
            },
        }
        body = json.dumps(payload).encode()
        resp = client.post(
            "/webhook/retell",
            content=body,
            headers={"x-retell-signature": "sig"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        mock_dispatch.assert_called_once()

    @patch("main.dispatch.start_dispatch")
    @patch("main.sms.validate_retell_signature", return_value=True)
    def test_real_retell_call_analyzed_format(self, mock_validate, mock_dispatch, client, conn, db):
        payload = {
            "event": "call_analyzed",
            "call": {
                "call_id": "retell-real-001",
                "transcript": "Agent: hello\nUser: my AC is not turning on",
                "call_analysis": {
                    "custom_analysis_data": {
                        "caller_name": "Beef",
                        "caller_phone": 5101234567,
                        "urgency": "emergency",
                        "service_needed": "AC repair",
                        "is_lead": True,
                        "service_address": "123 Main Street, Katy, TX 77493",
                        "Issue_description": "AC is not turning on at all",
                    },
                },
            },
        }
        body = json.dumps(payload).encode()
        resp = client.post(
            "/webhook/retell",
            content=body,
            headers={"x-retell-signature": "sig"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        job = db.get_job(conn, data["job_id"])
        assert job["customer_name"] == "Beef"
        assert job["phone"] == "+15101234567"
        assert job["address"] == "123 Main Street, Katy, TX 77493"
        assert job["service_type"] == "AC repair"
        assert job["issue_description"] == "AC is not turning on at all"
        assert job["priority"] == "emergency"
        mock_dispatch.assert_called_once()

    @patch("main.notifications.send_message")
    @patch("main.dispatch.start_dispatch")
    @patch("main.sms.validate_retell_signature", return_value=True)
    def test_ignores_call_ended_before_analysis(self, mock_validate, mock_dispatch, mock_notification, client):
        payload = {
            "event": "call_ended",
            "call": {
                "call_id": "retell-ended-001",
                "call_status": "ended",
                "transcript": "Agent: hello\nUser: I need AC service",
            },
        }
        body = json.dumps(payload).encode()
        resp = client.post(
            "/webhook/retell",
            content=body,
            headers={"x-retell-signature": "sig"},
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
        mock_dispatch.assert_not_called()
        mock_notification.assert_not_called()

    @patch("main.dispatch.start_dispatch")
    @patch("main.sms.validate_retell_signature", return_value=True)
    def test_top_level_retell_call_analysis_format(self, mock_validate, mock_dispatch, client):
        payload = {
            "event_type": "call_analyzed",
            "call_id": "retell-top-level-001",
            "call_analysis": {
                "custom_analysis_data": {
                    "caller_name": "Top Level",
                    "caller_phone": "5101234567",
                    "service_address": "456 Top St",
                    "service_needed": "Heating",
                    "issue_description": "No heat",
                },
            },
        }
        body = json.dumps(payload).encode()
        resp = client.post(
            "/webhook/retell",
            content=body,
            headers={"x-retell-signature": "sig"},
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        mock_dispatch.assert_called_once()

    @patch("main.sms.send_eddie_notification")
    @patch("main.notifications.send_message")
    @patch("main.dispatch.start_dispatch")
    @patch("main.sms.validate_retell_signature", return_value=True)
    def test_owner_vehicle_quote_call_is_forwarded_to_eddie_not_dispatched(
        self, mock_validate, mock_dispatch, mock_notification, mock_eddie_sms, client, conn, db
    ):
        payload = {
            "event": "call_analyzed",
            "call": {
                "call_id": "retell-vehicle-quote",
                "transcript": (
                    "Agent: Hi this is Kristi with Residential AC & Heating, how can I help you today?\n"
                    "User: Transfer to mister Eddie Berto.\n"
                    "User: Diego from Westside Chevrolet.\n"
                    "User: He just purchased for me a vehicle, so I need just to get a quote for the vehicle that he traded in."
                ),
                "call_analysis": {
                    "custom_analysis_data": {
                        "caller_name": "Diego",
                        "caller_phone": "+18137090266",
                        "service_address": "Katy, Texas",
                        "service_needed": "Vehicle trade-in quote",
                        "Issue_description": "Diego needs a quote for the vehicle he traded in.",
                        "is_lead": False,
                        "hvac_service_request": False,
                        "lead_status": "not_a_lead",
                        "owner_direct_request": True,
                        "dispatch_allowed": False,
                    },
                },
            },
        }
        body = json.dumps(payload).encode()

        resp = client.post(
            "/webhook/retell",
            content=body,
            headers={"x-retell-signature": "sig"},
        )

        assert resp.status_code == 200
        assert resp.json() == {"status": "skipped", "reason": "owner direct request"}
        mock_dispatch.assert_not_called()
        mock_notification.assert_called_once()
        assert "owner direct request" in mock_notification.call_args[0][0]
        mock_eddie_sms.assert_called_once()
        forwarded_text = mock_eddie_sms.call_args[0][0]
        assert "Caller asked for Eddie directly" in forwarded_text
        assert "Diego (+18137090266)" in forwarded_text
        assert "vehicle" in forwarded_text.lower()
        assert "Westside Chevrolet" in forwarded_text
        assert db.get_recent_jobs(conn) == []

    @patch("main.notifications.send_message")
    @patch("main.dispatch.start_dispatch")
    @patch("main.sms.validate_retell_signature", return_value=True)
    def test_retell_dispatch_allowed_false_blocks_contractor_dispatch(
        self, mock_validate, mock_dispatch, mock_notification, client, conn, db
    ):
        payload = {
            "event": "call_analyzed",
            "call": {
                "call_id": "retell-dispatch-false",
                "transcript": "User: My AC needs repair.",
                "call_analysis": {
                    "custom_analysis_data": {
                        "caller_name": "Review Caller",
                        "caller_phone": "+15551234567",
                        "service_address": "123 Main St, Katy, TX 77493",
                        "service_needed": "AC Repair",
                        "Issue_description": "AC is not cooling.",
                        "is_lead": True,
                        "hvac_service_request": True,
                        "lead_status": "qualified_service_lead",
                        "owner_direct_request": False,
                        "dispatch_allowed": False,
                    },
                },
            },
        }
        body = json.dumps(payload).encode()

        resp = client.post(
            "/webhook/retell",
            content=body,
            headers={"x-retell-signature": "sig"},
        )

        assert resp.status_code == 200
        assert resp.json() == {"status": "skipped", "reason": "dispatch not allowed"}
        mock_dispatch.assert_not_called()
        mock_notification.assert_called_once()
        assert db.get_recent_jobs(conn) == []

    @patch("main.sms.send_eddie_notification")
    @patch("main.notifications.send_message")
    @patch("main.dispatch.start_dispatch")
    @patch("main.sms.validate_retell_signature", return_value=True)
    def test_edilberto_direct_request_is_not_dispatched(
        self, mock_validate, mock_dispatch, mock_notification, mock_eddie_sms, client, conn, db
    ):
        payload = {
            "call_id": "retell-edilberto-direct",
            "customer_name": "Maria",
            "phone": "4805551212",
            "address": "123 Main St",
            "service_type": "AC Repair",
            "issue_description": "Caller is looking for Edilberto about billing.",
            "transcript": "User: Can I speak to Eddy? I am looking for Edilberto.",
        }
        body = json.dumps(payload).encode()

        resp = client.post(
            "/webhook/retell",
            content=body,
            headers={"x-retell-signature": "sig"},
        )

        assert resp.status_code == 200
        assert resp.json() == {"status": "skipped", "reason": "owner direct request"}
        mock_dispatch.assert_not_called()
        mock_notification.assert_called_once()
        mock_eddie_sms.assert_called_once()
        assert "Maria (+14805551212)" in mock_eddie_sms.call_args[0][0]
        assert db.get_recent_jobs(conn) == []

    @patch("main.notifications.send_message")
    @patch("main.dispatch.start_dispatch")
    @patch("main.sms.validate_retell_signature", return_value=True)
    def test_city_only_address_is_not_dispatchable(
        self, mock_validate, mock_dispatch, mock_notification, client
    ):
        payload = {
            "call_id": "retell-city-only",
            "customer_name": "City Caller",
            "phone": "+15551234567",
            "address": "Katy, TX 77493",
            "service_type": "AC Repair",
            "issue_description": "Unit not cooling",
        }
        body = json.dumps(payload).encode()

        resp = client.post(
            "/webhook/retell",
            content=body,
            headers={"x-retell-signature": "sig"},
        )

        assert resp.status_code == 200
        assert resp.json() == {"status": "skipped", "reason": "no service address provided"}
        mock_dispatch.assert_not_called()
        mock_notification.assert_called_once()


# ---------------------------------------------------------------------------
# /webhook/twilio
# ---------------------------------------------------------------------------

class TestTwilioWebhook:

    def test_invalid_signature_returns_403(self, client):
        resp = client.post(
            "/webhook/twilio",
            data={"From": "+15550001111", "Body": "yes 2pm", "MessageSid": "SM001"},
            headers={"x-twilio-signature": "badsig"},
        )
        assert resp.status_code == 403
        assert "text/xml" in resp.headers["content-type"]

    @patch("main.dispatch.process_contractor_reply")
    @patch("main.sms.validate_twilio_signature", return_value=True)
    def test_contractor_reply(self, mock_validate, mock_process, client, conn, db):
        # Create a job assigned to Jose
        job = db.create_job(
            conn, "Customer A", "+15551234567", "100 Main St",
            service_type="AC", issue_description="Broken",
        )
        db.update_job(conn, job["id"], status="contacting_contractor", current_contractor="Jose")

        resp = client.post(
            "/webhook/twilio",
            data={"From": "+15550001111", "Body": "yes 2pm", "MessageSid": "SM100"},
            headers={"x-twilio-signature": "valid"},
        )
        assert resp.status_code == 200
        assert "text/xml" in resp.headers["content-type"]
        mock_process.assert_called_once()
        # Check contractor_name arg
        call_args = mock_process.call_args
        assert call_args[0][2] == "Jose"  # contractor_name
        assert call_args[0][3] == "yes 2pm"  # body

    @patch("main.dispatch.process_contractor_reply")
    def test_contractor_reply_validates_https_proxy_url(self, mock_process, client, conn, db):
        job = db.create_job(
            conn, "Customer A", "+15551234567", "100 Main St",
            service_type="AC", issue_description="Broken",
        )
        db.update_job(conn, job["id"], status="contacting_contractor", current_contractor="Jose")

        params = {"From": "+15550001111", "Body": "yes 2pm", "MessageSid": "SM101"}
        signature = RequestValidator("test-twilio-token").compute_signature(
            "https://testserver/webhook/twilio",
            params,
        )

        resp = client.post(
            "/webhook/twilio",
            data=params,
            headers={
                "x-twilio-signature": signature,
                "x-forwarded-proto": "https",
            },
        )

        assert resp.status_code == 200
        mock_process.assert_called_once()

    @patch("main.sms.validate_twilio_signature", return_value=True)
    def test_contractor_reply_no_active_job(self, mock_validate, client):
        """Contractor sends SMS but has no active job — should just return TwiML."""
        resp = client.post(
            "/webhook/twilio",
            data={"From": "+15550001111", "Body": "hello", "MessageSid": "SM200"},
            headers={"x-twilio-signature": "valid"},
        )
        assert resp.status_code == 200
        assert "<Response></Response>" in resp.text

    @patch("main.dispatch.process_eddie_command")
    @patch("main.sms.validate_twilio_signature", return_value=True)
    def test_eddie_command(self, mock_validate, mock_cmd, client, conn, db):
        job = db.create_job(
            conn, "Customer B", "+15552222222", "200 Oak Ave",
            service_type="Heating", issue_description="No heat",
        )
        db.update_job(conn, job["id"], status="conditional_pending", current_contractor="Jose")

        resp = client.post(
            "/webhook/twilio",
            data={"From": "+15550009999", "Body": "OK", "MessageSid": "SM300"},
            headers={"x-twilio-signature": "valid"},
        )
        assert resp.status_code == 200
        mock_cmd.assert_called_once()
        assert mock_cmd.call_args[0][2] == "OK"

    @patch("main.dispatch.process_eddie_command")
    @patch("main.sms.validate_twilio_signature", return_value=True)
    def test_eddie_eta_command(self, mock_validate, mock_cmd, client, conn, db):
        job = db.create_job(
            conn, "Customer B", "+15552222222", "200 Oak Ave",
            service_type="Heating", issue_description="No heat",
        )
        db.update_job(conn, job["id"], status="accepted_waiting_eta", current_contractor="Jose")

        resp = client.post(
            "/webhook/twilio",
            data={"From": "+15550009999", "Body": "ETA 3-4pm", "MessageSid": "SM301"},
            headers={"x-twilio-signature": "valid"},
        )
        assert resp.status_code == 200
        mock_cmd.assert_called_once()
        assert mock_cmd.call_args[0][2] == "ETA 3-4pm"

    @patch("main.dispatch.process_eddie_command")
    @patch("main.sms.validate_twilio_signature", return_value=True)
    def test_eddie_command_with_job_prefix(self, mock_validate, mock_cmd, client, conn, db):
        job = db.create_job(
            conn, "Customer C", "+15553333333", "300 Pine Rd",
            service_type="AC", issue_description="Leak",
        )
        db.update_job(conn, job["id"], status="contacting_contractor", current_contractor="Mario")

        resp = client.post(
            "/webhook/twilio",
            data={
                "From": "+15550009999",
                "Body": f"JOB-{job['id']} CANCEL",
                "MessageSid": "SM400",
            },
            headers={"x-twilio-signature": "valid"},
        )
        assert resp.status_code == 200
        mock_cmd.assert_called_once()
        assert mock_cmd.call_args[0][2] == "CANCEL"

    @patch("main.sms.send_sms")
    @patch("main.sms.validate_twilio_signature", return_value=True)
    def test_eddie_unrecognized_command(self, mock_validate, mock_send, client):
        resp = client.post(
            "/webhook/twilio",
            data={"From": "+15550009999", "Body": "what?", "MessageSid": "SM500"},
            headers={"x-twilio-signature": "valid"},
        )
        assert resp.status_code == 200
        mock_send.assert_called_once()
        assert "Commands:" in mock_send.call_args[0][1]
        assert "ETA <time>" in mock_send.call_args[0][1]

    @patch("main.dispatch.process_customer_reply")
    @patch("main.sms.validate_twilio_signature", return_value=True)
    def test_customer_reply(self, mock_validate, mock_customer_reply, client, conn, db):
        job = db.create_job(
            conn, "Customer D", "+15554445555", "400 Cedar St",
            service_type="AC", issue_description="Broken",
        )
        db.update_job(conn, job["id"], status="contractor_confirmed", current_contractor="Jose")

        resp = client.post(
            "/webhook/twilio",
            data={"From": "+15554445555", "Body": "Can he come earlier?", "MessageSid": "SM550"},
            headers={"x-twilio-signature": "valid"},
        )

        assert resp.status_code == 200
        mock_customer_reply.assert_called_once()
        assert mock_customer_reply.call_args[0][1]["id"] == job["id"]
        assert mock_customer_reply.call_args[0][2] == "+15554445555"
        assert mock_customer_reply.call_args[0][3] == "Can he come earlier?"

    @patch("main.sms.validate_twilio_signature", return_value=True)
    def test_unknown_number_ignored(self, mock_validate, client):
        resp = client.post(
            "/webhook/twilio",
            data={"From": "+15559876543", "Body": "hello", "MessageSid": "SM600"},
            headers={"x-twilio-signature": "valid"},
        )
        assert resp.status_code == 200
        assert "<Response></Response>" in resp.text


# ---------------------------------------------------------------------------
# /dash/{slug}
# ---------------------------------------------------------------------------

class TestDashboard:

    def test_dashboard_correct_slug(self, client, conn, db):
        db.create_job(
            conn, "Dash Customer", "+15551111111", "999 Elm St",
            service_type="AC", issue_description="Test",
        )
        resp = client.get("/dash/test-dash")
        assert resp.status_code == 200
        assert "Dash Customer" in resp.text
        assert "999 Elm St" in resp.text
        assert 'id="phone-Customer"' in resp.text
        assert "Live mode" in resp.text

    def test_dashboard_wrong_slug_returns_404(self, client):
        resp = client.get("/dash/wrong-slug")
        assert resp.status_code == 404

    @patch("main.dispatch.upgrade_to_emergency")
    def test_urgent_action(self, mock_upgrade, client, conn, db):
        job = db.create_job(
            conn, "Urgent Customer", "+15554444444", "111 Urgent St",
            service_type="AC", issue_description="Emergency",
        )
        resp = client.post(f"/dash/test-dash/urgent/{job['id']}", follow_redirects=False)
        assert resp.status_code == 303
        assert "/dash/test-dash" in resp.headers["location"]
        mock_upgrade.assert_called_once()

    def test_urgent_wrong_slug_returns_404(self, client):
        resp = client.post("/dash/wrong/urgent/1", follow_redirects=False)
        assert resp.status_code == 404

    @patch("main.dispatch.process_eddie_command")
    def test_cancel_action(self, mock_cmd, client, conn, db):
        job = db.create_job(
            conn, "Cancel Customer", "+15555555555", "222 Cancel Ave",
            service_type="AC", issue_description="Cancel me",
        )
        resp = client.post(f"/dash/test-dash/cancel/{job['id']}", follow_redirects=False)
        assert resp.status_code == 303
        assert "/dash/test-dash" in resp.headers["location"]
        mock_cmd.assert_called_once()
        assert mock_cmd.call_args[0][2] == "CANCEL"

    def test_cancel_wrong_slug_returns_404(self, client):
        resp = client.post("/dash/wrong/cancel/1", follow_redirects=False)
        assert resp.status_code == 404

    def test_dashboard_waiting_eta_status_is_active(self, client, conn, db):
        job = db.create_job(
            conn, "Waiting Customer", "+15556667777", "333 Waiting Way",
            service_type="AC", issue_description="Waiting ETA",
        )
        db.update_job(conn, job["id"], status="accepted_waiting_eta", current_contractor="Jose")

        resp = client.get("/dash/test-dash")

        assert resp.status_code == 200
        assert "accepted waiting eta" in resp.text


# ---------------------------------------------------------------------------
# Dry-run test endpoints
# ---------------------------------------------------------------------------

class TestDryRunEndpoints:

    def test_customer_reply_requires_dry_run(self, client):
        resp = client.post(
            "/test/customer",
            json={"body": "Can he come earlier?"},
        )

        assert resp.status_code == 403

    @patch("main.dispatch.process_customer_reply")
    def test_customer_reply_by_job_id(self, mock_customer_reply, monkeypatch, client, conn, db):
        import main
        monkeypatch.setattr(main.config, "DRY_RUN", True)

        job = db.create_job(
            conn, "Test Customer", "+15551234567", "444 Test St",
            service_type="AC", issue_description="Test",
        )

        resp = client.post(
            "/test/customer",
            json={"job_id": job["id"], "body": "Can he come earlier?"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["job_id"] == job["id"]
        mock_customer_reply.assert_called_once()
        assert mock_customer_reply.call_args[0][2] == "+15551234567"
        assert mock_customer_reply.call_args[0][3] == "Can he come earlier?"

    def test_internal_customer_confirmation_scenario_fires(self, monkeypatch, client, conn, db):
        import main
        monkeypatch.setattr(main.config, "DRY_RUN", True)

        resp = client.post("/test/scenario/internal_customer_confirmation")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        job = db.get_job(conn, data["job_id"])
        assert job["customer_name"] == "Keith Test"
        assert job["phone"] == "+15551234567"
        assert job["address"] == "123 Test Street, Katy, TX 77493"
