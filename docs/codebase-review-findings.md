# Codebase Review Findings

Date: 2026-05-05

Context: dispatcher app where Retell webhooks create jobs, Twilio texts contractors based on dispatch rules, and Eddie receives summaries through Slack/Discord or SMS fallback.

These are review notes only. No fixes are implemented here.

## High Priority

### Emergency replies can route to the wrong contractor

Emergency dispatch texts all active contractors, but the job stores only one `current_contractor`: the first contacted contractor. Later decline or conditional replies do not always use the actual replying contractor when deciding the next state.

Relevant code:

- `dispatch.py`: `start_dispatch` sets `current_contractor` to `contacted[0]` for emergency jobs.
- `dispatch.py`: `process_contractor_reply` calls `escalate_to_next(conn, job)` on decline.
- `dispatch.py`: `escalate_to_next` advances from `job["current_contractor"]`, not the contractor who replied.
- `dispatch.py`: conditional replies update `status="conditional_pending"` but do not set `current_contractor` to the replying contractor.
- `dispatch.py`: Eddie `OK` confirms `job["current_contractor"]`.

Possible impact:

- A non-current contractor can decline and still be re-texted or become current.
- A conditional reply from Raul or Mario could be confirmed as Jose or another current contractor.

Suggested fix later:

- In `process_contractor_reply`, when a contractor replies on an emergency job, update or pass through the replying contractor for decline/conditional paths.
- Consider tracking per-contractor dispatch state instead of relying on one `current_contractor`.

### Eddie notification failures can block or partially break dispatch

`start_dispatch` notifies Eddie before texting contractors. `_notify_eddie` lets Slack/Discord/SMS failures raise. If chat webhooks are down, a valid Retell job can fail before contractors are contacted.

There is also a retry problem on inbound replies: the inbound message is logged for Twilio dedup before all side effects finish. If the job update succeeds but the Eddie notification fails, a Twilio retry can be ignored as a duplicate, leaving Eddie without the confirmation/escalation notice.

Relevant code:

- `dispatch.py`: `_notify_eddie`
- `dispatch.py`: `start_dispatch`
- `dispatch.py`: `process_contractor_reply`
- `notifications.py`: `send_message`

Possible impact:

- Valid jobs can fail to dispatch during Slack/Discord webhook outage.
- Eddie can miss important confirmation/escalation messages after a partial failure.

Suggested fix later:

- Make Eddie notifications non-blocking or best-effort for initial dispatch.
- Persist notification delivery attempts separately from inbound SMS dedup.
- Alert on notification failures without stopping contractor SMS.

### Non-`call_analyzed` Retell events may dispatch if they contain an address

The Retell webhook ignores non-analysis events only when no address is present. If Retell sends a `call_ended` or other pre-analysis event with an address, the app can create and dispatch a job before full analysis is available. A later `call_analyzed` event with the same call id is deduped and will not update the job.

Relevant code:

- `main.py`: `webhook_retell` guard around `event_type`
- `db.py`: `create_job` dedup by `retell_call_id`

Possible impact:

- Contractors may receive incomplete or premature job details.
- Transcript, summary, and recording details from the final analysis may never be saved.

Suggested fix later:

- Only dispatch on `call_analyzed`, except for explicit local/test payloads with no event type.
- If a duplicate call id arrives with richer analysis, update the existing job fields before deciding whether to dispatch.

## Medium Priority

### Jobs can dispatch without a customer phone

The dispatch skip logic requires a service address but does not require a usable customer phone. The current workflow tells the technician to contact the customer directly, so a missing phone makes the contractor handoff incomplete.

Relevant code:

- `main.py`: `_dispatch_skip_reason`
- `dispatch.py`: `_build_job_sms`

Possible impact:

- Contractors can receive a job with `Customer: ... (N/A)` and no way to contact the customer directly.

Suggested fix later:

- Require a valid customer phone before contractor dispatch.
- Route missing-phone leads to Eddie/chat as human review instead of dispatching.

### Contractor replies are ambiguous across multiple active jobs

Inbound contractor SMS is matched to the most recent active job for that contractor phone. The direct current-contractor lookup includes confirmed jobs, and replies do not parse job numbers from the body.

Relevant code:

- `main.py`: `webhook_twilio` contractor reply routing
- `db.py`: `get_active_job_for_contractor`

Possible impact:

- A late contractor reply can attach to the wrong job if the contractor has multiple active or recent jobs.
- Replies after confirmation can still route to a confirmed job.

Suggested fix later:

- Parse `Job #123` or `#123` from contractor replies when present.
- Exclude `contractor_confirmed` from direct active-job lookup unless the reply includes that job id.
- Consider requiring job ids in contractor response instructions.

### Polling alerts can spam, and heartbeat business hours use UTC

If polling is re-enabled, stale-job alerts can fire every polling pass for the same job. The heartbeat check uses UTC hour instead of local Central time.

Relevant code:

- `dispatch.py`: `run_polling_loop`
- `dispatch.py`: `_should_send_heartbeat_alert`

Possible impact:

- Repeated stale-job alerts can spam Eddie/chat.
- No-new-jobs heartbeat can run outside intended local business hours.

Suggested fix later:

- Add per-job stale alert suppression or store last stale alert time.
- Use `America/Chicago` business hours for heartbeat checks.

## Test Coverage Gaps

The current non-eval suite passed during review:

```text
202 passed, 27 deselected
```

Recommended future tests:

- Emergency decline from a non-current contractor.
- Emergency conditional reply from a non-current contractor.
- Slack/Discord outage during initial `start_dispatch`.
- Twilio retry after partial side-effect failure.
- Retell `call_ended` with address before `call_analyzed`.
- Qualified HVAC lead with missing customer phone.
- Contractor has multiple active jobs and replies with/without a job id.
