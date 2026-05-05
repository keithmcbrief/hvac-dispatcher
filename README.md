# HVAC Dispatcher

HVAC Dispatcher is a FastAPI service for coordinating contractor availability after an inbound HVAC service call.

The current workflow is:

1. Retell AI handles the customer call and posts a call-analysis webhook.
2. The app creates a dispatch job in SQLite.
3. The technician is texted through Twilio with the job details and customer phone number.
4. The technician contacts the customer directly and handles scheduling.
5. Contractor replies, if any, are classified with a regex fast path and an OpenAI fallback.
6. Eddie is notified when a contractor confirms, declines, gives a condition, or needs manual handling.
7. Customer replies are relayed to Eddie/chat notifications for manual handling.

Job polling, automatic follow-ups, and automatic customer confirmation texts are paused by default. The app does not auto-answer customer replies. It relays the exact customer message to Eddie/chat notifications so Eddie can jump in.

## Architecture

- Python 3.12
- FastAPI and Uvicorn
- SQLite with WAL mode
- Twilio SMS
- Retell post-call webhook intake
- Optional chat webhook notifications, including free Discord webhooks
- Fly.io deployment with a persistent volume

Important files:

- `main.py`: FastAPI routes, webhook handlers, dashboard, dry-run endpoints
- `dispatch.py`: contractor dispatch state machine and optional polling loop
- `classifier.py`: reply classifier
- `db.py`: SQLite schema and query helpers
- `sms.py`: Twilio helpers and webhook signature validation
- `notifications.py`: outbound Discord, Slack, or generic webhook notifications
- `slack.py`: Slack request validation and legacy notification aliases
- `templates/dashboard.html`: dry-run dispatch dashboard
- `test_scenarios/`: local dry-run Retell payload fixtures

## Local Setup

Create a virtual environment and install dependencies:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy the example environment file:

```bash
cp .env.example .env
```

Fill in the required values in `.env`:

- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_NUMBER`
- `OPENAI_API_KEY`
- `RETELL_API_KEY`
- `EDDIE_PHONE`
- `JOSE_PHONE`
- `MARIO_PHONE`
- `RAUL_PHONE`

Jose is the first contractor by priority. Set `JOSE_ACTIVE=false` if he needs to be paused temporarily.

For local simulation without real SMS:

```bash
DRY_RUN=true SKIP_SIGNATURE_VALIDATION=true \
python3.12 -m uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```

Open the dashboard at:

```bash
python3.12 -c "from config import DASHBOARD_SLUG; print(f'http://localhost:8080/dash/{DASHBOARD_SLUG}')"
```

The dashboard can run an internal end-to-end simulation without texting Eddie,
contractors, or customers. In dry-run mode, fire the
`internal_customer_confirmation` scenario, reply as `Mario` or `Raul`,
and reply as the `Customer` from the dashboard phone panels.

Useful API calls for the same flow:

```bash
curl -X POST http://localhost:8080/test/clear
curl -X POST http://localhost:8080/test/scenario/internal_customer_confirmation
curl -X POST http://localhost:8080/test/reply \
  -H "Content-Type: application/json" \
  -d '{"contractor":"Mario","body":"yes"}'
curl -X POST http://localhost:8080/test/reply \
  -H "Content-Type: application/json" \
  -d '{"contractor":"Mario","body":"5pm"}'
curl -X POST http://localhost:8080/test/customer \
  -H "Content-Type: application/json" \
  -d '{"body":"Can he come earlier?"}'
```

## Tests

Run the normal test suite:

```bash
python3.12 -m pytest -q -m "not eval"
```

The classifier eval suite calls the real OpenAI API:

```bash
python3.12 -m pytest tests/test_classifier_eval.py -m eval
```

## Deployment

The repo includes a `Dockerfile` and `fly.toml` for Fly.io.

Create a persistent volume before first deploy:

```bash
fly volumes create data -a hvac-dispatcher -r ord -s 1
```

Set production secrets with `fly secrets set`; do not commit secrets to git.

```bash
fly secrets set -a hvac-dispatcher \
  TWILIO_ACCOUNT_SID="..." \
  TWILIO_AUTH_TOKEN="..." \
  TWILIO_NUMBER="+1..." \
  OPENAI_API_KEY="..." \
  RETELL_API_KEY="..." \
  EDDIE_PHONE="+1..." \
  JOSE_PHONE="+1..." \
  MARIO_PHONE="+1..." \
  RAUL_PHONE="+1..." \
  JOSE_ACTIVE=true \
  MARIO_ACTIVE=true \
  RAUL_ACTIVE=true \
  NOTIFICATIONS_ENABLED=true \
  NOTIFICATION_PROVIDER=both \
  SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..." \
  DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..." \
  DASHBOARD_SLUG="choose-a-long-random-secret-slug"
```

Use `NOTIFICATION_PROVIDER=both` during the migration to post every outbound notification to both Slack and Discord. Set it to `discord`, `slack`, or `generic` when you only want one destination. If both Slack and Discord webhook URLs are present and `NOTIFICATION_PROVIDER` is unset, the app defaults to `both`.

Optional legacy automation switches:

```bash
fly secrets set -a hvac-dispatcher \
  JOB_POLLING_ENABLED=true \
  CUSTOMER_CONFIRMATION_SMS_ENABLED=true
```

Deploy a single Machine:

```bash
fly deploy -a hvac-dispatcher --ha=false
```

This service is designed for one running instance because it uses local SQLite. If `JOB_POLLING_ENABLED=true`, it also runs an in-process polling loop.

## Emergency Switches

Mute system/error alerts immediately if chat notifications start getting spammed by operational alerts:

```bash
fly secrets set -a hvac-dispatcher SYSTEM_ALERTS_ENABLED=false
```

This does not stop customer/job notifications, transcripts, or contractor texts. It only mutes messages sent through `[HVAC DISPATCH ALERT]`, such as stale-job, heartbeat, and polling-loop alerts.

Re-enable system alerts after the issue is fixed:

```bash
fly secrets set -a hvac-dispatcher SYSTEM_ALERTS_ENABLED=true
```

Disable only the no-new-jobs heartbeat:

```bash
fly secrets set -a hvac-dispatcher HEARTBEAT_HOURS=0
```

## Security Notes

- `.env` is ignored and must not be committed.
- Raw Retell webhook payload logging is disabled by default.
- Set `SAVE_WEBHOOK_LOGS=true` only for local replay/debugging.
- `webhook_logs/`, local DB files, and tests are excluded from production Docker builds.
- Do not set `SKIP_SIGNATURE_VALIDATION=true` in production.
- Use Fly secrets or another secret manager for runtime credentials.
