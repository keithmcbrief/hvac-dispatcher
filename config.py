"""Configuration module for Eddie's HVAC Contractor Dispatch Agent."""

import os
import secrets

from dotenv import load_dotenv

load_dotenv(override=True)


def _env_bool(name: str, default: str = "") -> bool:
    return os.getenv(name, default).lower() in ("true", "1", "yes", "on")


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
SYSTEM_ALERTS_ENABLED = os.getenv("SYSTEM_ALERTS_ENABLED", "true").lower() not in (
    "false",
    "0",
    "no",
    "off",
)

# Customer-facing copy
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "Residential AC & Heating")

# Eddie's phone
EDDIE_PHONE = os.getenv("EDDIE_PHONE", "")

# Contractors - ordered by priority.
# Set JOSE_ACTIVE=false to pause Jose temporarily.
CONTRACTORS = {
    "Jose": {
        "phone": os.getenv("JOSE_PHONE", ""),
        "priority": 1,
        "active": _env_bool("JOSE_ACTIVE", "true"),
    },
    "Mario": {
        "phone": os.getenv("MARIO_PHONE", ""),
        "priority": 2,
        "active": _env_bool("MARIO_ACTIVE", "true"),
    },
    "Raul": {
        "phone": os.getenv("RAUL_PHONE", ""),
        "priority": 3,
        "active": _env_bool("RAUL_ACTIVE", "true"),
    },
}

# Reverse lookup: phone -> contractor name.
# Includes inactive contractors so replies to already-assigned jobs still route.
CONTRACTOR_PHONES = {info["phone"]: name for name, info in CONTRACTORS.items() if info["phone"]}

# Dry-run mode — suppress real Twilio SMS, log everything to DB
DRY_RUN = _env_bool("DRY_RUN")

# Current operating mode: Retell intake sends the job to the technician once,
# and the technician contacts the customer directly.
JOB_POLLING_ENABLED = _env_bool("JOB_POLLING_ENABLED", "false")
CUSTOMER_CONFIRMATION_SMS_ENABLED = _env_bool("CUSTOMER_CONFIRMATION_SMS_ENABLED", "false")

# Timing constants (env-overridable for fast testing)
FOLLOW_UP_INTERVAL_SECONDS = int(os.getenv("FOLLOW_UP_INTERVAL_SECONDS", "300"))
MAX_ATTEMPTS_PER_CONTRACTOR = 3
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
STALENESS_ALERT_MINUTES = 15
JOB_TTL_HOURS = 24
HEARTBEAT_HOURS = int(os.getenv("HEARTBEAT_HOURS", "12"))
HEARTBEAT_ALERT_INTERVAL_HOURS = int(os.getenv("HEARTBEAT_ALERT_INTERVAL_HOURS", "24"))

# Chat notifications (replaces Eddie SMS when enabled)
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
NOTIFICATION_WEBHOOK_URL = os.getenv("NOTIFICATION_WEBHOOK_URL", "")
_DEFAULT_NOTIFICATION_PROVIDER = "slack"
if SLACK_WEBHOOK_URL:
    _DEFAULT_NOTIFICATION_PROVIDER = "slack"
if DISCORD_WEBHOOK_URL:
    _DEFAULT_NOTIFICATION_PROVIDER = "discord"
if SLACK_WEBHOOK_URL and DISCORD_WEBHOOK_URL:
    _DEFAULT_NOTIFICATION_PROVIDER = "both"
NOTIFICATION_PROVIDER = os.getenv(
    "NOTIFICATION_PROVIDER",
    _DEFAULT_NOTIFICATION_PROVIDER,
)
NOTIFICATIONS_ENABLED = os.getenv(
    "NOTIFICATIONS_ENABLED",
    os.getenv("SLACK_ENABLED", ""),
).lower() in ("true", "1", "yes")

# Slack compatibility and optional Slack command webhook validation
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
SLACK_ENABLED = NOTIFICATIONS_ENABLED and NOTIFICATION_PROVIDER.lower() in ("slack", "both")

# Dashboard
DASHBOARD_SLUG = os.getenv("DASHBOARD_SLUG") or secrets.token_hex(4)

# Database path
DB_PATH = os.getenv("DB_PATH") or "dispatch.db"
