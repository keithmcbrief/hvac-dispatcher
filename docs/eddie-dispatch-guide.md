# Eddie Dispatch Guide

This guide explains what happens after Kristi takes a call, how contractors are contacted, what Eddie sees in Slack, and what contractors should text back.

## What Eddie Sees In Slack

Every valid customer call sends a Slack message to Eddie with the job details:

- Job number
- Service type
- Customer name and phone number
- Service address
- Issue description
- Recording link, when Retell provides one
- Full call transcript, when Retell provides one
- Status updates as contractors are contacted, followed up with, confirmed, or unavailable

If Kristi receives a call that is not dispatchable, Eddie still gets a Slack notice. Examples:

- No service address was collected
- The call was marked as not a lead
- The caller was spam, wrong number, or not a real service request

Those skipped-call Slack notices also include the transcript when available, so Eddie can review what happened.

## Contractor Priority

Contractors are prioritized in this order:

1. Jose
2. Mario
3. Raul

For normal jobs, the system works down that list one contractor at a time.

For emergency jobs, the system texts all available contractors at the same time.

## Normal Job Flow

A normal job is a standard service request that is not marked urgent or emergency.

1. Kristi finishes the call.
2. Retell sends the analyzed call data to the dispatch app.
3. Eddie gets a Slack message with the job details and full transcript.
4. Jose gets the first contractor text.
5. If Jose accepts with an ETA, the job is confirmed with Jose, the customer gets a confirmation text, and Eddie gets a Slack confirmation.
6. If Jose accepts without an ETA, the system asks Jose for an ETA and does not text the customer yet.
7. If Jose declines, the job moves to Mario immediately.
8. If Jose does not reply, the system follows up with Jose.
9. If Jose still does not reply after all attempts, the job moves to Mario.
10. The same process repeats for Mario, then Raul.
11. If nobody accepts, Eddie gets a Slack alert that no contractor is available and the job needs manual handling.

## Emergency Job Flow

An emergency job is a call marked urgent, emergency, or ASAP by Kristi/Retell.

1. Kristi finishes the call.
2. Retell sends the analyzed call data to the dispatch app.
3. Eddie gets a Slack message marked emergency with the job details and full transcript.
4. Jose, Mario, and Raul all get the job text at the same time.
5. The first contractor to clearly accept with an ETA gets assigned the job.
6. If a contractor accepts without an ETA, the system asks that contractor for an ETA and does not text the customer yet.
7. Once an ETA is provided, the customer gets a confirmation text and Eddie gets a Slack confirmation.
8. Any other contractors who were contacted get a follow-up text saying the job has been taken and no action is needed.
9. If nobody responds after all attempts, Eddie gets a Slack alert that no contractor responded and the job needs manual handling.

## Timing And Follow-Ups

Current timing:

- The app checks for follow-ups every 30 seconds.
- Each follow-up interval is 5 minutes.
- Each contractor gets up to 3 total contact attempts:
  - Initial job text
  - Follow-up #2 after about 5 minutes
  - Follow-up #3 after about 10 minutes

For a normal job:

- Jose gets up to about 10 minutes before the system moves to Mario.
- Mario gets up to about 10 minutes before the system moves to Raul.
- Raul gets up to about 10 minutes before the system marks the job as no contractor available.
- Worst case with no replies is about 30 minutes, plus up to 30 seconds for the polling loop.

If a contractor declines, the system does not wait for the full retry window. It moves to the next contractor immediately.

For an emergency job:

- All contractors are texted immediately.
- Unreplied contractors get follow-ups after about 5 minutes and again after about 10 minutes.
- If nobody accepts after that, Eddie gets a manual-handling Slack alert.

## Contractor Text Message

Contractors receive a text like this:

```text
New Job (#12)
Service: AC repair
Address: 123 Main Street, Katy, TX 77493
Customer: John Smith
Issue: AC is not cooling

When can you be there?
```

Follow-up texts look like this:

```text
Following up on Job #12: AC repair at 123 Main Street, Katy, TX 77493. Can you make it?
```

When an emergency job is taken by another contractor, the remaining contractors receive:

```text
Update on Job #12 (AC repair at 123 Main Street, Katy, TX 77493): This job has been taken. No action needed. Thanks!
```

If a contractor accepts without an ETA, the contractor receives:

```text
Thanks. What time can you arrive for Job #12? Please reply with an ETA like "5pm" or "in 45 minutes".
```

## Customer Text Message

The customer is texted only after a contractor confirms with a usable ETA.

Example:

```text
Residential AC & Heating: Jose is confirmed for 5pm for your AC repair at 123 Main Street, Katy, TX 77493. Reply here if anything changes.
```

If the contractor replies that they are already on the way, the customer gets:

```text
Residential AC & Heating: Jose is on the way for your AC repair at 123 Main Street, Katy, TX 77493. Reply here if anything changes.
```

If the customer replies, the app does not auto-answer. It relays the exact message to Slack with the job number, customer, address, assigned contractor, and ETA.

## Expected Contractor Replies

Contractors should reply in plain language by text.

Accepted examples:

```text
Yes
Yes today at 5pm
I can be there in 30 minutes
On my way
I can take it
```

Declined examples:

```text
No
Can't make it
I'm busy
Not available
Pass
```

Conditional examples:

```text
I can do it after 6pm
I can go if customer can wait until tomorrow
I can take it but only after my current job
```

Unclear examples:

```text
Maybe
Where is it?
Call me
What is this?
```

The system uses simple matching for obvious yes/no replies and AI classification for more detailed replies like “Yes today at 5pm.”

## What Happens By Reply Type

If a contractor accepts:

- If the contractor gave an ETA, the job is marked confirmed.
- The customer gets a confirmation text.
- Eddie gets a Slack confirmation.
- The confirmation includes the contractor name, ETA, and the customer text that was sent.
- Other already-contacted contractors are told the job has been taken.

If a contractor accepts without an ETA:

- The job waits for that contractor's ETA.
- The customer is not texted yet.
- The contractor gets an ETA request text.
- Eddie gets a Slack alert that the contractor accepted but timing is missing.

If a contractor declines:

- For normal jobs, the system moves to the next contractor immediately.
- Eddie does not need to intervene unless all contractors decline or nobody is available.

If a contractor gives a conditional answer:

- The job pauses in a conditional state.
- Eddie gets a Slack alert with the condition.
- Eddie can decide whether to accept the condition or move to the next contractor.

If a contractor gives an unclear answer:

- Eddie gets a Slack alert with the unclear reply.
- Eddie can handle it manually or wait for the contractor to clarify.

## Eddie's Role

Eddie does not need to monitor every text manually. Slack is the visibility layer.

Eddie should watch for:

- New-call Slack alerts
- Emergency alerts
- Confirmed contractor alerts
- Conditional replies that require a decision
- No-contractor-available alerts
- Calls that were skipped because the address was missing or the call was not a lead

Once a contractor confirms with an ETA, the app sends the initial customer confirmation text. Eddie should still watch Slack for customer replies or questions.

## Important Notes

- The app texts the customer only after a contractor provides an ETA.
- The app does not auto-answer customer replies.
- Customer replies are relayed to Slack exactly as received.
- Slack receives full call visibility, including transcripts when Retell provides them.
- Contractor routing depends on each contractor texting back from the phone number configured for them.
- If a contractor replies from a different phone number, the system will not recognize them automatically.
- If Retell does not collect an address, the call is not dispatched to contractors. Eddie gets a Slack notice instead.
- The live database is stored on Fly in `/data/dispatch.db`.
