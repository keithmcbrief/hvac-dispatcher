# Eddie Dispatch Guide

Kristi -> Dispatch App -> Contractors -> Slack

Prepared by Spotter Digital - spotterdigital.ai

## What You See In Slack

Every valid customer call sends a Slack message to you with the job details:

- Job number
- Service type
- Customer name and phone number
- Service address
- Issue description
- Recording link, when available
- Full call transcript, when available
- Status updates as contractors are contacted, followed up with, confirmed, or unavailable
- Customer confirmation status after a contractor provides an ETA
- Any customer replies after the confirmation text is sent

If Kristi receives a call that is not dispatchable, you still get a Slack notice. This includes calls where no service address was collected, the call was not a lead, or it was spam/wrong number. Those notices also include the transcript when available, so you can review what happened.

## Contractor Priority

Contractors are contacted in this order:

| Priority | Contractor | Notes |
| --- | --- | --- |
| 1st | Jose | First choice for all jobs |
| 2nd | Mario | If Jose declines or does not reply |
| 3rd | Raul | Last in the chain |

Normal jobs: the system works down the list one contractor at a time.

Emergency jobs: all available contractors are texted simultaneously.

## Normal Job Flow

A normal job is any standard service request that is not marked urgent or emergency.

1. Kristi finishes the call.
2. Retell sends the analyzed call data to the dispatch app.
3. You get a Slack message with the job details and full transcript.
4. Jose gets the first contractor text.
5. If Jose accepts with an ETA, the job is confirmed, the customer gets a confirmation text, and you get a Slack confirmation.
6. If Jose accepts without an ETA, the system asks Jose for an ETA and does not text the customer yet.
7. Once Jose provides the ETA, the customer gets the confirmation text and you get the final Slack confirmation.
8. If Jose declines, the job moves to Mario immediately.
9. If Jose does not reply, the system follows up with Jose.
10. If Jose still does not reply after all attempts, the job moves to Mario.
11. The same process repeats for Mario, then Raul.
12. If nobody accepts, you get a Slack alert that no contractor is available and the job needs manual handling.

## Emergency Job Flow

An emergency job is any call marked urgent, emergency, or ASAP by Kristi.

1. Kristi finishes the call.
2. Retell sends the analyzed call data to the dispatch app.
3. You get a Slack message marked EMERGENCY with the job details and full transcript.
4. Jose, Mario, and Raul all get the job text at the same time.
5. The first contractor to accept with an ETA gets assigned the job.
6. If the first contractor accepts without an ETA, the system asks that contractor for an ETA and does not text the customer yet.
7. Once the ETA is provided, the customer gets the confirmation text and you get a Slack confirmation.
8. Other contacted contractors get a text saying the job has been taken.
9. If nobody responds after all attempts, you get a Slack alert for manual handling.

## Timing And Follow-Ups

| Setting | Value |
| --- | --- |
| Follow-up check interval | Every 30 seconds |
| Time between follow-ups | 5 minutes |
| Total contact attempts per contractor | 3: initial + 2 follow-ups |
| Time per contractor before moving on | About 10 minutes |
| Worst-case total, normal job with no replies | About 30 minutes |

Key: if a contractor declines, the system moves to the next one immediately. It does not wait for the full retry window.

For emergency jobs, all contractors are texted immediately. Unreplied contractors still get follow-ups at about 5 and 10 minutes.

## What Contractors Receive

Initial job text:

```text
New Job (#12)
Service: AC repair
Address: 123 Main Street, Katy, TX 77493
Customer: John Smith
Issue: AC is not cooling

When can you be there?
```

Follow-up text:

```text
Following up on Job #12: AC repair at 123 Main Street, Katy, TX 77493. Can you make it?
```

If a contractor accepts without an ETA:

```text
Thanks. What time can you arrive for Job #12? Please reply with an ETA like "5pm" or "in 45 minutes".
```

Emergency job taken notice:

```text
Update on Job #12 (AC repair at 123 Main Street, Katy, TX 77493): This job has been taken. No action needed. Thanks!
```

## How Contractors Reply

Contractors reply in plain language by text. The system uses simple matching for obvious yes/no replies and AI classification for more detailed responses.

| Type | Examples | What Happens |
| --- | --- | --- |
| Accepted with ETA | "Yes 5pm" / "On my way" / "I can be there in 30 min" | Job confirms. Customer gets a confirmation text. You get Slack confirmation with ETA. |
| Accepted without ETA | "Yes" / "I can take it" | Job waits for ETA. Contractor gets an ETA request. Customer is not texted yet. You get a Slack notice. |
| Declined | "No" / "Can't make it" / "Pass" | Job moves to next contractor immediately. |
| Conditional | "I can do it after 6pm" / "Only if customer can wait" | Job pauses. You get a Slack alert with the condition to decide. |
| Unclear | "Maybe" / "Where is it?" / "Call me" | You get a Slack alert. Handle manually or wait for clarification. |

## What Customers Receive

The customer is texted only after a contractor confirms with a usable ETA.

Example confirmation text:

```text
Residential AC & Heating: Jose is confirmed for 5pm for your AC repair at 123 Main Street, Katy, TX 77493. Reply here if anything changes.
```

If the contractor says they are on the way:

```text
Residential AC & Heating: Jose is on the way for your AC repair at 123 Main Street, Katy, TX 77493. Reply here if anything changes.
```

If a contractor accepts without an ETA, the customer is not texted yet. The system first asks the contractor for a time.

## Customer Replies

The app does not automatically answer customer replies.

If the customer replies to the Twilio number, the exact message is relayed to Slack with:

- Job number
- Customer name and phone number
- Service address
- Assigned contractor
- ETA, when available
- The exact customer message

Example Slack relay:

```text
Customer reply for Job #12

Customer: John Smith (+15551234567)
Address: 123 Main Street, Katy, TX 77493
Assigned: Jose
ETA: 5pm

Message:
"Can he come earlier?"
```

This lets you jump in manually when the customer asks a question, changes timing, or needs help.

## Your Role

You do not need to monitor every text manually. Slack is your visibility layer.

Watch for:

- New-call Slack alerts
- Emergency alerts
- Confirmed contractor alerts
- Customer confirmation status
- Customer replies or questions
- Contractor acceptances that are missing an ETA
- Conditional replies that need your decision
- No-contractor-available alerts
- Skipped calls, such as missing address or not a lead

## After A Contractor Confirms

If the contractor provided an ETA, the app sends the initial customer confirmation text automatically.

If the contractor accepted without an ETA, the app asks the contractor for timing first and does not text the customer until the ETA is received.

You should still watch Slack for customer replies, customer questions, contractor conditions, and manual-handling alerts.

## Important Notes

- The app texts the customer only after a contractor provides an ETA.
- The app does not automatically answer customer replies.
- Customer replies are relayed to Slack exactly as received.
- Slack receives full call visibility, including transcripts when Retell provides them.
- Calls that are clearly not HVAC service, or that only include a city/state instead of a service address, are not dispatched to contractors.
- Calls asking for Eddie, Eddy, or Edilberto directly are not dispatched to contractors. Eddie gets a direct SMS with the caller details instead.
- Contractor phone numbers matter: routing depends on each contractor texting back from the phone number configured for them.
- A contractor reply from a different number will not be recognized automatically.
- If Retell does not collect an address, the call is not dispatched to contractors. You get a Slack notice instead.
- The live database is stored on Fly in `/data/dispatch.db`.

Spotter Digital - spotterdigital.ai - Katy, TX
