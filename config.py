"""Configuration module for Eddie's HVAC Contractor Dispatch Agent."""

import os
import secrets

from dotenv import load_dotenv

load_dotenv(override=True)

# Twilio — support both TWILIO_* and legacy naming
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID") or os.getenv("ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN") or os.getenv("AUTH_TOKEN", "")
TWILIO_NUMBER = os.getenv("TWILIO_NUMBER", "")

# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Retell (signature validation)
RETELL_API_KEY = os.getenv("RETELL_API_KEY", "")
RETELL_WEBHOOK_TOKEN = os.getenv("RETELL_WEBHOOK_TOKEN", "")
SAVE_WEBHOOK_LOGS = os.getenv("SAVE_WEBHOOK_LOGS", "").lower() in ("true", "1", "yes")

# Alert / builder phone
BUILDER_PHONE = os.getenv("BUILDER_PHONE", "")

# Eddie's phone
EDDIE_PHONE = os.getenv("EDDIE_PHONE", "")

# Contractors — ordered by priority
CONTRACTORS = {
    "Jose": {"phone": os.getenv("JOSE_PHONE", ""), "priority": 1},
    "Mario": {"phone": os.getenv("MARIO_PHONE", ""), "priority": 2},
    "Raul": {"phone": os.getenv("RAUL_PHONE", ""), "priority": 3},
}

# Reverse lookup: phone → contractor name
CONTRACTOR_PHONES = {info["phone"]: name for name, info in CONTRACTORS.items() if info["phone"]}

# Dry-run mode — suppress real Twilio SMS, log everything to DB
DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("true", "1", "yes")

# Timing constants (env-overridable for fast testing)
FOLLOW_UP_INTERVAL_SECONDS = int(os.getenv("FOLLOW_UP_INTERVAL_SECONDS", "300"))
MAX_ATTEMPTS_PER_CONTRACTOR = 3
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
STALENESS_ALERT_MINUTES = 15
JOB_TTL_HOURS = 24
HEARTBEAT_HOURS = int(os.getenv("HEARTBEAT_HOURS", "12"))
HEARTBEAT_ALERT_INTERVAL_HOURS = int(os.getenv("HEARTBEAT_ALERT_INTERVAL_HOURS", "24"))

# Slack (replaces Eddie SMS when enabled)
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
SLACK_ENABLED = os.getenv("SLACK_ENABLED", "").lower() in ("true", "1", "yes")

# Dashboard
DASHBOARD_SLUG = os.getenv("DASHBOARD_SLUG") or secrets.token_hex(4)

# Database path
DB_PATH = os.getenv("DB_PATH") or "dispatch.db"
