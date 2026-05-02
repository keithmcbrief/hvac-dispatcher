# Eddie Dispatch Guide

Kristi -> Dispatch App -> Contractors -> Chat Notifications

Prepared by Spotter Digital - spotterdigital.ai

## What You See In Chat Notifications

Every valid customer call sends a Discord/chat message to you with the job details:

- Job number
- Service type
- Customer name and phone number
- Service address
- Issue description
- Recording link, when available
- Full call transcript, when available
- Status updates when a contractor replies
- Customer contact status showing that the technician is expected to contact the customer directly

If Kristi receives a call that is not dispatchable, you still get a chat notice. This includes calls where no service address was collected, the call was not a lead, or it was spam/wrong number. Those notices also include the transcript when available, so you can review what happened.

## Contractor Priority

Contractors are contacted in this order:

| Priority | Contractor | Notes |
| --- | --- | --- |
| Paused | Jose | Temporarily removed from outbound dispatch |
| 1st active | Mario | First choice while Jose is paused |
| 2nd active | Raul | Backup after Mario |

Normal jobs: Mario gets the job text first while Jose is paused.

Emergency jobs: all available contractors are texted simultaneously.

## Normal Job Flow

A normal job is any standard service request that is not marked urgent or emergency.

1. Kristi finishes the call.
2. Retell sends the analyzed call data to the dispatch app.
3. You get a chat message with the job details and full transcript.
4. Mario gets the first contractor text.
5. Mario contacts the customer directly using the phone number in the job text.
6. If Mario replies, the reply is logged and Eddie gets the relevant status notification.
7. The app does not automatically follow up, move the job to another contractor, or text the customer.

## Emergency Job Flow

An emergency job is any call marked urgent, emergency, or ASAP by Kristi.

1. Kristi finishes the call.
2. Retell sends the analyzed call data to the dispatch app.
3. You get a chat message marked EMERGENCY with the job details and full transcript.
4. Mario and Raul get the job text at the same time while Jose is paused.
5. The contractors contact the customer directly using the phone number in the job text.
6. If a contractor replies that they accepted the job, Eddie gets a confirmation notification.
7. Other contacted contractors get a text saying the job has been taken.
8. The app does not automatically follow up or text the customer.

## Timing And Follow-Ups

| Setting | Value |
| --- | --- |
| Job polling | Paused |
| Contractor follow-ups | Paused |
| Automatic escalation on no reply | Paused |
| Automatic customer confirmation texts | Paused |

Polling can be restored later by enabling `JOB_POLLING_ENABLED=true`. Automatic customer confirmation texts can be restored separately with `CUSTOMER_CONFIRMATION_SMS_ENABLED=true`.

## What Contractors Receive

Initial job text:

```text
New Job (#12)
Service: AC repair
Address: 123 Main Street, Katy, TX 77493
Customer: John Smith (+15551234567)
Issue: AC is not cooling

Contact the customer directly to confirm and schedule. Reply here only if Eddie needs an update.
```

Emergency job taken notice:

```text
Update on Job #12 (AC repair at 123 Main Street, Katy, TX 77493): This job has been taken. No action needed. Thanks!
```

## How Contractors Reply

Contractors reply in plain language by text. The system uses simple matching for obvious yes/no replies and AI classification for more detailed responses.

| Type | Examples | What Happens |
| --- | --- | --- |
| Accepted with ETA | "Yes 5pm" / "On my way" / "I can be there in 30 min" | Job confirms. Customer is not texted automatically. You get chat confirmation with ETA. |
| Accepted without ETA | "Yes" / "I can take it" | Job confirms. Customer is not texted automatically. |
| Declined | "No" / "Can't make it" / "Pass" | Eddie gets visibility. Automatic escalation is paused. |
| Conditional | "I can do it after 6pm" / "Only if customer can wait" | Job pauses. You get a chat alert with the condition to decide. |
| Unclear | "Maybe" / "Where is it?" / "Call me" | You get a chat alert. Handle manually or wait for clarification. |

## What Customers Receive

Customers are not texted automatically by the app in the current workflow.

The technician receives the customer phone number in the job text and contacts the customer directly.

## Customer Replies

The app does not automatically answer customer replies.

If the customer replies to the Twilio number, the exact message is relayed to chat notifications with:

- Job number
- Customer name and phone number
- Service address
- Assigned contractor
- ETA, when available
- The exact customer message

Example chat relay:

```text
Customer reply for Job #12

Customer: John Smith (+15551234567)
Address: 123 Main Street, Katy, TX 77493
Assigned: Mario
ETA: 5pm

Message:
"Can he come earlier?"
```

This lets you jump in manually when the customer asks a question, changes timing, or needs help.

## Your Role

You do not need to monitor every text manually. Chat notifications are your visibility layer.

Watch for:

- New-call chat alerts
- Emergency alerts
- Contractor reply alerts
- Confirmation alerts if a contractor replies
- Conditional replies that need your decision
- Skipped calls, such as missing address or not a lead

## After A Contractor Confirms

The technician contacts the customer directly. The app does not send the customer confirmation text automatically.

You should still watch chat notifications for customer replies, customer questions, contractor conditions, and manual-handling alerts.

## Important Notes

- The app does not text the customer automatically in the current workflow.
- The app does not automatically answer customer replies.
- Customer replies are relayed to chat notifications exactly as received.
- Chat notifications receive full call visibility, including transcripts when Retell provides them.
- Calls that are clearly not HVAC service, or that only include a city/state instead of a service address, are not dispatched to contractors.
- Calls asking for Eddie, Eddy, or Edilberto directly are not dispatched to contractors. Eddie gets a direct SMS with the caller details instead.
- Contractor phone numbers matter: routing depends on each contractor texting back from the phone number configured for them.
- A contractor reply from a different number will not be recognized automatically.
- Job polling, automatic follow-ups, and automatic no-reply escalation are paused.
- If Retell does not collect an address, the call is not dispatched to contractors. You get a chat notice instead.
- The live database is stored on Fly in `/data/dispatch.db`.

Spotter Digital - spotterdigital.ai - Katy, TX
