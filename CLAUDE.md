# Eddie's HVAC Contractor Dispatch Agent

## What This Is

AI agent that replaces Eddie's manual texting workflow for his HVAC company. Retell AI voice receptionist takes inbound calls, sends webhook to this agent, which then texts contractors (Jose > Mario > Raul priority order) to find availability, follows up every 5 min, and notifies Eddie when someone confirms.

V1 scope: contractor coordination only. Eddie handles customer confirmation manually.

## Architecture

- Python 3.12 + FastAPI, single-instance server
- SQLite (WAL mode, raw sqlite3, no ORM) for state
- Twilio SMS for all messaging
- Retell AI post-call webhook for job intake
- 12-state deterministic dispatch state machine in `dispatch.py`
- Regex-first reply classification with GPT-4o-mini fallback in `classifier.py`
- Polling loop (every 30s) for follow-ups, immediate dispatch from webhook
- Fly.io deployment with persistent volume

## Retell Webhook Format (REAL, from production)

Retell sends `call_analyzed` events. Customer data lives in `call.call_analysis.custom_analysis_data`, NOT in top-level fields or `retell_llm_dynamic_variables`.

**Field names Retell uses (note the inconsistencies):**
- `caller_name` (not `customer_name`)
- `caller_phone` (can be INTEGER, not string — e.g. `51012354321` not `"+15101235432"`)
- `service_address` (not `address`)
- `service_needed` (not `service_type`)
- `Issue_description` (capital I — not `issue_description`)
- `urgency` (not `priority`) — values: "Emergency", "Normal"
- `is_lead` (boolean) — false means spam/not-a-real-customer

**Real webhook structure:**
```json
{
  "event": "call_analyzed",
  "call": {
    "call_id": "call_e4de78de605a5b51ac8dad75107",
    "call_type": "web_call",
    "agent_id": "agent_b99e4837f8613ff317f87acc8c",
    "call_status": "ended",
    "start_timestamp": 1774671081684,
    "end_timestamp": 1774671176522,
    "duration_ms": 94838,
    "transcript": "Agent: ... User: ...",
    "transcript_object": [ ... ],
    "call_analysis": {
      "call_summary": "The caller, Keith, reported that...",
      "in_voicemail": false,
      "user_sentiment": "Neutral",
      "call_successful": true,
      "custom_analysis_data": {
        "caller_name": "Keith",
        "caller_phone": 51012354321,
        "service_address": "123 Main Street, Katy, Texas 77493",
        "service_needed": "AC Repair",
        "Issue_description": "AC is running but not cooling since yesterday",
        "urgency": "Emergency",
        "is_lead": true
      }
    }
  }
}
```

The parser in `main.py` (lines 100-127) handles all the field name mismatches and format quirks.

## Local Testing

### Start the server (dry-run mode, no Twilio needed)
```bash
DRY_RUN=true SKIP_SIGNATURE_VALIDATION=true \
  FOLLOW_UP_INTERVAL_SECONDS=15 POLL_INTERVAL_SECONDS=5 \
  python3.12 -m uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```

In dry-run mode:
- No real SMS sent (no Twilio credentials needed)
- Follow-ups every 15s, poll every 5s (fast cascade)
- `/test/reply` and `/test/eddie` endpoints enabled
- All messages logged to DB with `DRYRUN_` SID prefix

### Start the server (real SMS mode)
```bash
SKIP_SIGNATURE_VALIDATION=true python3.12 -m uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```

### Expose with ngrok (for Retell/Twilio webhooks)
```bash
ngrok http 8080
```

### Replay a saved webhook payload
Set `SAVE_WEBHOOK_LOGS=true` locally to save raw Retell payloads under `webhook_logs/` for replay testing. Replay them with:
```bash
curl -s -X POST http://localhost:8080/webhook/retell \
  -H "Content-Type: application/json" \
  -d @webhook_logs/retell_call_e4de78de605a5b51ac8dad75107_1774671521.json
```

### Run test scenarios (pre-built)
```bash
./test_scenarios/run.sh              # list all scenarios
./test_scenarios/run.sh all          # run all 15 scenarios
./test_scenarios/run.sh emergency    # run 5 emergency scenarios
./test_scenarios/run.sh normal       # run 5 normal scenarios
./test_scenarios/run.sh spam         # run 5 spam scenarios
./test_scenarios/run.sh spam_01      # run one specific scenario
./test_scenarios/run.sh clear        # clear the database
```

### Simulate contractor replies (dry-run mode)
```bash
# Jose accepts
curl -s -X POST http://localhost:8080/test/reply \
  -H "Content-Type: application/json" \
  -d '{"contractor": "Jose", "body": "Yes I can be there at 3pm"}'

# Mario declines
curl -s -X POST http://localhost:8080/test/reply \
  -H "Content-Type: application/json" \
  -d '{"contractor": "Mario", "body": "Sorry, busy today"}'

# Target a specific job
curl -s -X POST http://localhost:8080/test/reply \
  -H "Content-Type: application/json" \
  -d '{"contractor": "Jose", "body": "I can do it", "job_id": 3}'
```

### Simulate Eddie commands (dry-run mode)
```bash
curl -s -X POST http://localhost:8080/test/eddie \
  -H "Content-Type: application/json" \
  -d '{"command": "NEXT"}'

# Target a specific job
curl -s -X POST http://localhost:8080/test/eddie \
  -H "Content-Type: application/json" \
  -d '{"command": "CANCEL", "job_id": 1}'
```

### Simulate a Twilio SMS reply (real SMS mode, costs money)
```bash
curl -s -X POST http://localhost:8080/webhook/twilio \
  -d "From=%2B12145551234&Body=Yes+I+can+be+there+at+3pm&MessageSid=SM_test_$(date +%s)"
```

### Check the dashboard
```bash
python3.12 -c "from config import DASHBOARD_SLUG; print(f'http://localhost:8080/dash/{DASHBOARD_SLUG}')"
```

### Run tests
```bash
python3.12 -m pytest -x -q
```

## Key Gotchas (learned the hard way)

1. **`caller_phone` can be an integer** — Retell sometimes sends `51012354321` instead of `"+15101235432"`. The parser converts to string and prepends `+`.
2. **`Issue_description` has a capital I** — Retell's field naming is inconsistent. The `_extract()` helper tries multiple variants.
3. **`SKIP_SIGNATURE_VALIDATION=true`** is required for local testing — otherwise all webhook requests return 403.
4. **`DB_PATH=` (empty string) in .env** breaks config — `os.getenv("DB_PATH", "dispatch.db")` returns empty string, not the default. Fixed with `os.getenv("DB_PATH") or "dispatch.db"`.
5. **Shell env vars override .env** — python-dotenv doesn't override existing env vars. If `OPENAI_API_KEY` is set in your shell profile, the .env value is ignored.
6. **Shared contractor phone numbers** — If multiple contractors share a phone (test setup), the Twilio webhook handler falls back to checking all contractors with that number, not just the first match.
7. **Test scenarios use `retell_llm_dynamic_variables` format** — The 15 test scenarios in `test_scenarios/` use the simpler format, which still works because the parser checks both `custom_analysis_data` and `retell_llm_dynamic_variables`.

## File Overview

| File | What |
|------|------|
| `main.py` | FastAPI app, webhook routes, Retell parser |
| `dispatch.py` | State machine, polling loop, all dispatch logic |
| `classifier.py` | Regex + GPT-4o-mini reply classification |
| `db.py` | SQLite schema, queries, dedup logic |
| `sms.py` | Twilio send/validate, error alerts |
| `config.py` | Env var loading, contractor config, timing constants |
| `templates/dashboard.html` | Job dashboard UI |
| `test_scenarios/` | 15 JSON payloads + runner script |
| `webhook_logs/` | Optional local raw Retell payload captures when `SAVE_WEBHOOK_LOGS=true` |
