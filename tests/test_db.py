"""Tests for db module."""

import importlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def _use_temp_db(monkeypatch, tmp_path):
    """Point DB_PATH to a temp file and reinitialise for every test."""
    db_file = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_file)

    # Write empty .env so load_dotenv doesn't load the real one
    env_file = tmp_path / ".env"
    env_file.write_text("")
    monkeypatch.chdir(tmp_path)

    import config
    importlib.reload(config)

    import db
    importlib.reload(db)

    db.init_db()
    yield db


@pytest.fixture
def conn(_use_temp_db):
    """Return a fresh connection for each test."""
    c = _use_temp_db.get_connection()
    yield c
    c.close()


@pytest.fixture
def db(_use_temp_db):
    return _use_temp_db


# ---- init / connection ---------------------------------------------------

def test_init_creates_tables(conn):
    tables = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    ]
    assert "jobs" in tables
    assert "messages" in tables


def test_wal_mode(conn):
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    # WAL mode should be set (could be "wal" or already "wal")
    assert mode.lower() == "wal"


# ---- create_job -----------------------------------------------------------

def test_create_job(db, conn):
    job = db.create_job(conn, "Alice", "+15551234567", "123 Main St")
    assert job["customer_name"] == "Alice"
    assert job["phone"] == "+15551234567"
    assert job["status"] == "new"
    assert job["priority"] == "normal"
    assert job["attempt_count"] == 0


def test_create_job_with_all_fields(db, conn):
    job = db.create_job(
        conn,
        "Bob",
        "+15559876543",
        "456 Oak Ave",
        service_type="AC repair",
        issue_description="No cold air",
        priority="emergency",
        retell_call_id="call_123",
    )
    assert job["service_type"] == "AC repair"
    assert job["issue_description"] == "No cold air"
    assert job["priority"] == "emergency"
    assert job["retell_call_id"] == "call_123"


def test_create_job_dedup_retell(db, conn):
    job1 = db.create_job(conn, "Alice", "+15551234567", "123 Main St", retell_call_id="call_dup")
    job2 = db.create_job(conn, "Bob", "+15559999999", "999 Other St", retell_call_id="call_dup")
    assert job1["id"] == job2["id"]
    assert job2["customer_name"] == "Alice"  # original preserved


# ---- get_job --------------------------------------------------------------

def test_get_job(db, conn):
    created = db.create_job(conn, "Alice", "+15551234567", "123 Main St")
    fetched = db.get_job(conn, created["id"])
    assert fetched["id"] == created["id"]


def test_get_job_not_found(db, conn):
    assert db.get_job(conn, 9999) is None


# ---- update_job -----------------------------------------------------------

def test_update_job(db, conn):
    job = db.create_job(conn, "Alice", "+15551234567", "123 Main St")
    updated = db.update_job(conn, job["id"], status="awaiting_reply", current_contractor="Jose")
    assert updated["status"] == "awaiting_reply"
    assert updated["current_contractor"] == "Jose"
    # updated_at should be set (may equal created_at within same second)
    assert updated["updated_at"] is not None


# ---- get_jobs_needing_action ----------------------------------------------

def test_get_jobs_needing_action(db, conn):
    job = db.create_job(conn, "Alice", "+15551234567", "123 Main St")
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    db.update_job(conn, job["id"], status="awaiting_reply", next_action_at=past)

    results = db.get_jobs_needing_action(conn)
    assert len(results) == 1
    assert results[0]["id"] == job["id"]


def test_get_jobs_needing_action_excludes_future(db, conn):
    job = db.create_job(conn, "Alice", "+15551234567", "123 Main St")
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    db.update_job(conn, job["id"], status="awaiting_reply", next_action_at=future)

    assert len(db.get_jobs_needing_action(conn)) == 0


def test_get_jobs_needing_action_excludes_wrong_status(db, conn):
    job = db.create_job(conn, "Alice", "+15551234567", "123 Main St")
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    db.update_job(conn, job["id"], status="completed", next_action_at=past)

    assert len(db.get_jobs_needing_action(conn)) == 0


# ---- get_stale_jobs -------------------------------------------------------

def test_get_stale_jobs(db, conn):
    job = db.create_job(conn, "Alice", "+15551234567", "123 Main St")
    # Set next_action_at to 20 minutes ago
    old_time = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
    db.update_job(conn, job["id"], status="awaiting_reply", next_action_at=old_time)

    # Should be stale if threshold is 15 minutes
    results = db.get_stale_jobs(conn, 15)
    assert len(results) == 1

    # Should NOT be stale with 25-minute threshold
    assert len(db.get_stale_jobs(conn, 25)) == 0


def test_get_stale_jobs_excludes_terminal(db, conn):
    job = db.create_job(conn, "Alice", "+15551234567", "123 Main St")
    old_time = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
    db.update_job(conn, job["id"], status="completed", next_action_at=old_time)

    assert len(db.get_stale_jobs(conn, 15)) == 0


# ---- get_expired_confirmed_jobs -------------------------------------------

def test_get_expired_confirmed_jobs(db, conn):
    job = db.create_job(conn, "Alice", "+15551234567", "123 Main St")
    old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")
    db.update_job(conn, job["id"], status="contractor_confirmed")
    # Manually set updated_at to old time
    conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (old_time, job["id"]))
    conn.commit()

    results = db.get_expired_confirmed_jobs(conn, 24)
    assert len(results) == 1


def test_get_expired_confirmed_jobs_excludes_recent(db, conn):
    job = db.create_job(conn, "Alice", "+15551234567", "123 Main St")
    db.update_job(conn, job["id"], status="contractor_confirmed")

    assert len(db.get_expired_confirmed_jobs(conn, 24)) == 0


# ---- get_active_job_for_contractor ----------------------------------------

def test_get_active_job_for_contractor(db, conn):
    job = db.create_job(conn, "Alice", "+15551234567", "123 Main St")
    db.update_job(conn, job["id"], current_contractor="Jose", status="awaiting_reply")

    result = db.get_active_job_for_contractor(conn, "Jose")
    assert result is not None
    assert result["id"] == job["id"]


def test_get_active_job_for_contractor_excludes_terminal(db, conn):
    job = db.create_job(conn, "Alice", "+15551234567", "123 Main St")
    db.update_job(conn, job["id"], current_contractor="Jose", status="completed")

    assert db.get_active_job_for_contractor(conn, "Jose") is None


# ---- get_most_recent_active_job -------------------------------------------

def test_get_most_recent_active_job(db, conn):
    db.create_job(conn, "Alice", "+15551234567", "123 Main St")
    job2 = db.create_job(conn, "Bob", "+15559876543", "456 Oak Ave")

    result = db.get_most_recent_active_job(conn)
    assert result["id"] == job2["id"]


def test_get_most_recent_active_job_skips_terminal(db, conn):
    job1 = db.create_job(conn, "Alice", "+15551234567", "123 Main St")
    job2 = db.create_job(conn, "Bob", "+15559876543", "456 Oak Ave")
    db.update_job(conn, job2["id"], status="completed")

    result = db.get_most_recent_active_job(conn)
    assert result["id"] == job1["id"]


def test_get_most_recent_active_job_none(db, conn):
    assert db.get_most_recent_active_job(conn) is None


# ---- get_recent_jobs ------------------------------------------------------

def test_get_recent_jobs(db, conn):
    for i in range(5):
        db.create_job(conn, f"Customer {i}", f"+1555{i:07d}", f"{i} Main St")

    results = db.get_recent_jobs(conn, limit=3)
    assert len(results) == 3
    # Most recent (highest id) first
    assert results[0]["customer_name"] == "Customer 4"
    assert results[1]["customer_name"] == "Customer 3"
    assert results[2]["customer_name"] == "Customer 2"


# ---- log_message ----------------------------------------------------------

def test_log_message(db, conn):
    job = db.create_job(conn, "Alice", "+15551234567", "123 Main St")
    msg = db.log_message(conn, job["id"], "outbound", "Hello contractor")
    assert msg["direction"] == "outbound"
    assert msg["body"] == "Hello contractor"
    assert msg["job_id"] == job["id"]


def test_log_message_all_fields(db, conn):
    job = db.create_job(conn, "Alice", "+15551234567", "123 Main St")
    msg = db.log_message(
        conn,
        job["id"],
        "inbound",
        "Yes I can do it",
        contractor_name="Jose",
        parsed_intent="accept",
        twilio_sid="SM123",
        twilio_message_sid="MM456",
    )
    assert msg["contractor_name"] == "Jose"
    assert msg["parsed_intent"] == "accept"
    assert msg["twilio_sid"] == "SM123"
    assert msg["twilio_message_sid"] == "MM456"


def test_log_message_dedup(db, conn):
    job = db.create_job(conn, "Alice", "+15551234567", "123 Main St")
    msg1 = db.log_message(conn, job["id"], "inbound", "Hello", twilio_message_sid="MM_DUP")
    msg2 = db.log_message(conn, job["id"], "inbound", "Different body", twilio_message_sid="MM_DUP")
    assert msg1["id"] == msg2["id"]
    assert msg2["body"] == "Hello"  # original preserved


# ---- get_last_job_created_at & count_jobs_since ---------------------------

def test_get_last_job_created_at_empty(db, conn):
    assert db.get_last_job_created_at(conn) is None


def test_get_last_job_created_at(db, conn):
    db.create_job(conn, "Alice", "+15551234567", "123 Main St")
    result = db.get_last_job_created_at(conn)
    assert result is not None


def test_count_jobs_since(db, conn):
    db.create_job(conn, "Alice", "+15551234567", "123 Main St")
    db.create_job(conn, "Bob", "+15559876543", "456 Oak Ave")

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    assert db.count_jobs_since(conn, yesterday) == 2

    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    assert db.count_jobs_since(conn, tomorrow) == 0
