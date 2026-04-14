# HVAC Dispatcher

HVAC Dispatcher is a FastAPI service for coordinating contractor availability after an inbound HVAC service call.

The current workflow is:

1. Retell AI handles the customer call and posts a call-analysis webhook.
2. The app creates a dispatch job in SQLite.
3. Contractors are texted through Twilio in priority order.
4. Contractor replies are classified with a regex fast path and an OpenAI fallback.
5. Eddie is notified when a contractor confirms, declines, gives a condition, or needs manual handling.

V1 coordinates contractors only. Customer confirmation remains manual.

## Architecture

- Python 3.12
- FastAPI and Uvicorn
- SQLite with WAL mode
- Twilio SMS
- Retell post-call webhook intake
- Optional Slack notifications
- Fly.io deployment with a persistent volume

Important files:

- `main.py`: FastAPI routes, webhook handlers, dashboard, dry-run endpoints
- `dispatch.py`: contractor dispatch state machine and polling loop
- `classifier.py`: reply classifier
- `db.py`: SQLite schema and query helpers
- `sms.py`: Twilio helpers and webhook signature validation
- `slack.py`: Slack webhook and request validation helpers
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

For local simulation without real SMS:

```bash
DRY_RUN=true SKIP_SIGNATURE_VALIDATION=true \
FOLLOW_UP_INTERVAL_SECONDS=15 POLL_INTERVAL_SECONDS=5 \
python3.12 -m uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```

Open the dashboard at:

```bash
python3.12 -c "from config import DASHBOARD_SLUG; print(f'http://localhost:8080/dash/{DASHBOARD_SLUG}')"
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
  DASHBOARD_SLUG="choose-a-long-random-secret-slug"
```

Deploy a single Machine:

```bash
fly deploy -a hvac-dispatcher --ha=false
```

This service is designed for one running instance because it uses local SQLite and an in-process polling loop.

## Security Notes

- `.env` is ignored and must not be committed.
- Raw Retell webhook payload logging is disabled by default.
- Set `SAVE_WEBHOOK_LOGS=true` only for local replay/debugging.
- `webhook_logs/`, local DB files, and tests are excluded from production Docker builds.
- Do not set `SKIP_SIGNATURE_VALIDATION=true` in production.
- Use Fly secrets or another secret manager for runtime credentials.
