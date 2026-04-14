"""SQLite database wrapper for Eddie's HVAC Contractor Dispatch Agent."""

import sqlite3
from datetime import datetime, timezone

from config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name TEXT NOT NULL,
    phone TEXT NOT NULL,
    address TEXT NOT NULL,
    service_type TEXT,
    issue_description TEXT,
    priority TEXT NOT NULL DEFAULT 'normal' CHECK(priority IN ('normal','emergency')),
    status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','contacting_contractor','awaiting_reply','follow_up_1','follow_up_2','escalating','contractor_confirmed','conditional_pending','no_contractor_available','completed','cancelled','send_failed')),
    current_contractor TEXT,
    attempt_count INTEGER DEFAULT 0,
    contractor_response TEXT,
    confirmed_time TEXT,
    next_action_at TEXT,
    retell_call_id TEXT UNIQUE,
    transcript TEXT,
    recording_url TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    direction TEXT NOT NULL CHECK(direction IN ('inbound','outbound')),
    contractor_name TEXT,
    body TEXT NOT NULL,
    parsed_intent TEXT,
    twilio_sid TEXT,
    twilio_message_sid TEXT UNIQUE,
    timestamp TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init_db():
    """Create tables if they don't exist. Sets WAL mode and busy_timeout."""
    conn = get_connection()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def get_connection():
    """Return a connection with row_factory = sqlite3.Row."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# ---------------------------------------------------------------------------
# Query functions
# ---------------------------------------------------------------------------


def create_job(
    conn,
    customer_name,
    phone,
    address,
    service_type=None,
    issue_description=None,
    priority="normal",
    retell_call_id=None,
    transcript=None,
    recording_url=None,
):
    """Create a new job. If retell_call_id already exists, return the existing job (dedup)."""
    if retell_call_id:
        existing = conn.execute(
            "SELECT * FROM jobs WHERE retell_call_id = ?", (retell_call_id,)
        ).fetchone()
        if existing:
            return existing

    cur = conn.execute(
        """INSERT INTO jobs
           (customer_name, phone, address, service_type, issue_description, priority, retell_call_id, transcript, recording_url)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (customer_name, phone, address, service_type, issue_description, priority, retell_call_id, transcript, recording_url),
    )
    conn.commit()
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (cur.lastrowid,)).fetchone()


def get_job(conn, job_id):
    """Return job row or None."""
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def get_jobs_needing_action(conn):
    """Return jobs where next_action_at <= now and status is actionable."""
    return conn.execute(
        """SELECT * FROM jobs
           WHERE next_action_at <= datetime('now')
             AND status IN ('contacting_contractor', 'awaiting_reply', 'follow_up_1', 'follow_up_2', 'send_failed')"""
    ).fetchall()


def get_stale_jobs(conn, minutes):
    """Return jobs where next_action_at is overdue by more than `minutes` minutes."""
    return conn.execute(
        """SELECT * FROM jobs
           WHERE next_action_at IS NOT NULL
             AND datetime(next_action_at, '+' || ? || ' minutes') < datetime('now')
             AND status NOT IN ('completed', 'cancelled', 'no_contractor_available')""",
        (minutes,),
    ).fetchall()


def get_expired_confirmed_jobs(conn, hours):
    """Return jobs in 'contractor_confirmed' state for more than `hours` hours."""
    return conn.execute(
        """SELECT * FROM jobs
           WHERE status = 'contractor_confirmed'
             AND datetime(updated_at, '+' || ? || ' hours') < datetime('now')""",
        (hours,),
    ).fetchall()


def get_active_job_for_contractor(conn, contractor_name):
    """Return the most recent active job for a contractor.

    Checks current_contractor first, then falls back to any active job
    where this contractor was sent a message (handles emergency dispatch
    where multiple contractors are texted but only one is current_contractor).
    """
    # Direct match on current_contractor
    job = conn.execute(
        """SELECT * FROM jobs
           WHERE current_contractor = ?
             AND status NOT IN ('completed', 'cancelled', 'no_contractor_available')
           ORDER BY id DESC
           LIMIT 1""",
        (contractor_name,),
    ).fetchone()
    if job:
        return job

    # Fallback: find active job where this contractor was texted
    return conn.execute(
        """SELECT j.* FROM jobs j
           JOIN messages m ON m.job_id = j.id
           WHERE m.contractor_name = ? AND m.direction = 'outbound'
             AND j.status NOT IN ('completed', 'cancelled', 'no_contractor_available', 'contractor_confirmed')
           ORDER BY j.id DESC
           LIMIT 1""",
        (contractor_name,),
    ).fetchone()


def get_most_recent_active_job(conn):
    """Return the most recent job that is not in a terminal state."""
    return conn.execute(
        """SELECT * FROM jobs
           WHERE status NOT IN ('completed', 'cancelled', 'no_contractor_available')
           ORDER BY id DESC
           LIMIT 1"""
    ).fetchone()


def get_recent_jobs(conn, limit=50):
    """Return last N jobs ordered by id DESC."""
    return conn.execute(
        "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


ALLOWED_JOB_COLUMNS = {
    "status", "current_contractor", "attempt_count", "contractor_response",
    "confirmed_time", "next_action_at", "priority", "updated_at",
}


def update_job(conn, job_id, **kwargs):
    """Update job fields, always sets updated_at. Returns the updated row."""
    invalid_cols = set(kwargs) - ALLOWED_JOB_COLUMNS
    if invalid_cols:
        raise ValueError(f"Invalid column(s) for update_job: {invalid_cols}")

    # All timestamps must be UTC.
    kwargs["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [job_id]
    conn.execute(f"UPDATE jobs SET {set_clause} WHERE id = ?", values)
    conn.commit()
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def log_message(
    conn,
    job_id,
    direction,
    body,
    contractor_name=None,
    parsed_intent=None,
    twilio_sid=None,
    twilio_message_sid=None,
):
    """Insert a message row. If twilio_message_sid already exists (dedup), return existing."""
    if twilio_message_sid:
        existing = conn.execute(
            "SELECT * FROM messages WHERE twilio_message_sid = ?", (twilio_message_sid,)
        ).fetchone()
        if existing:
            return existing

    cur = conn.execute(
        """INSERT INTO messages
           (job_id, direction, contractor_name, body, parsed_intent, twilio_sid, twilio_message_sid)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (job_id, direction, contractor_name, body, parsed_intent, twilio_sid, twilio_message_sid),
    )
    conn.commit()
    return conn.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()


def get_last_job_created_at(conn):
    """Return the created_at of the most recent job, or None."""
    row = conn.execute("SELECT created_at FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
    return row["created_at"] if row else None


def count_jobs_since(conn, since_datetime):
    """Return count of jobs created after the given datetime."""
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM jobs WHERE created_at > ?", (since_datetime,)
    ).fetchone()
    return row["cnt"]
