# TODOS

## V1 Improvements

### Cancel command confirmation
**What:** Add SMS confirmation step for CANCEL command to prevent accidental cancellations.
**Why:** Fat-fingering `JOB-42 CANCEL` kills a live dispatch with no undo.
**How:** System replies "Cancel Job #42 for [customer]? Reply YES to confirm." Only CANCEL needs this. OK/NEXT/URGENT are non-destructive.
**Depends on:** Inbound SMS routing implementation.
**Added:** 2026-03-27 (eng review, outside voice flagged)

### Litestream SQLite backup
**What:** Add Litestream to continuously replicate SQLite to S3.
**Why:** Fly.io persistent volumes are local to one machine. Machine failure = total data loss.
**How:** Add Litestream binary to Docker image, configure S3 bucket, auto-restore on startup. ~$0.02/month.
**Depends on:** Fly.io deployment working, S3 bucket provisioned.
**Added:** 2026-03-27 (eng review, outside voice flagged as biggest architectural risk)

## V2: Customer-Facing Automation

### Customer confirmation and rescheduling
**What:** After contractor confirms, agent texts customer to book. Handles reschedules by re-coordinating with contractor.
**Why:** Eliminates remaining ~20% of Eddie's manual texting. Completes the full dispatch loop.
**How:** Add customer-facing state machine, NLP for customer replies ("can we do Thursday instead?"), rescheduling logic that re-enters contractor dispatch.
**Depends on:** V1 working reliably for 1-2 weeks.
**Added:** 2026-03-27 (office-hours, deferred from V1 scope)
