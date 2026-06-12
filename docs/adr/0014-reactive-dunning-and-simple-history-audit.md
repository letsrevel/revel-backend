# ADR-0014: Reactive Dunning + simple-history Audit for Subscriptions

## Status

Accepted

## Context

Phase 4 of the subscription system needed two pieces that traditionally
become large infrastructure projects:

1. **Dunning** — retrying failed renewal payments and eventually closing out
   subscriptions that don't recover.
2. **Audit trail** — who changed what on a subscription, plan, or payment,
   and when.

A naive implementation drops a per-subscription retry orchestrator into
Celery (custom retry schedule, exponential backoff, terminal-failure handler)
and stands up a bespoke audit model with manual writes from every service
function. Both have hidden maintenance costs: retry policies drift from
Stripe's behavior, audit writes get forgotten on new code paths.

## Decision

### Reactive dunning

**Let Stripe drive retries; mirror state via webhooks.** No custom retry
orchestrator. For ONLINE subscriptions:

| Stripe event | Local effect | Notification |
|---|---|---|
| `invoice.paid` | period_start/end updated; PENDING/PAST_DUE → ACTIVE; `MembershipPayment(SUCCEEDED)` | `RENEWAL_SUCCEEDED` (gated to true renewals — see below) |
| `invoice.payment_failed` | ACTIVE/PENDING → PAST_DUE; `MembershipPayment(FAILED)` | `PAYMENT_FAILED` |
| `customer.subscription.deleted` | EXPIRED + stamp `expired_at` | `SUBSCRIPTION_EXPIRED` |
| `customer.subscription.updated` (`cancel_at_period_end=true`, first time) | mirror flag | `CANCELLATION_CONFIRMED` (when not already locally set) |
| `charge.refunded` | mark `MembershipPayment` REFUNDED; auto-cancel if full refund of current period | `CANCELLATION_CONFIRMED` (only if auto-cancel triggered) |

For OFFLINE subscriptions, the daily `expire_subscriptions_past_grace` beat
task plays the same role: `ACTIVE` past `current_period_end` →
`PAST_DUE` (fires `PAYMENT_FAILED`) → past the org's grace window →
`EXPIRED` (fires `SUBSCRIPTION_EXPIRED`).

#### Idempotency gates

Re-delivered webhooks must not double-fire notifications:

- `MembershipPayment.update_or_create(stripe_invoice_id=...)` makes the row
  itself idempotent. The `_dispatch_invoice_notifications` helper uses the
  `payment_created` flag plus the captured `prior_status` to suppress
  duplicates.
- `RENEWAL_SUCCEEDED` is gated on `prior_status in {ACTIVE, PAST_DUE}` so
  the first invoice of a brand-new subscription (PENDING → ACTIVE) and any
  invoice that is part of a revival pass do **not** double-fire. The
  revival service additionally passes
  `dispatch_renewal_notification=False` to `record_payment` for OFFLINE
  revivals.
- `CANCELLATION_CONFIRMED` fires on a real transition only: when staff
  triggers the local cancel, the local row flips first → notification
  dispatches → Stripe call → webhook arrives with the flag already True →
  no transition → no second dispatch.

### Audit via `django-simple-history`

**`HistoricalRecords()` on the four subscription models, no custom audit
service.** Already enabled and admin-registered in the project via
`unfold.contrib.simple_history`; using it for subscriptions is a 4-line
change per model.

- `MembershipSubscriptionPlan`, `MembershipSubscription`,
  `MembershipPayment`, and `CustomerProfile` each gain a `history` manager.
- The `SimpleHistoryAdmin` mixin is added to the corresponding ModelAdmin
  classes so operators see a "History" tab with diffs.
- No backfill — history starts at first save after the Phase 4 migration.

Semantic events (member-visible state transitions, refunds, revivals) are
also captured as `MembershipPayment` rows and as structured log lines from
`logger.info(...)` in the service layer, which gives a queryable timeline
even when the row diff isn't enough.

## Consequences

- **Pro (dunning):** No retry policy of our own to maintain. Stripe Smart
  Retries handle cadence, exponential backoff, and final failure — we only
  mirror what Stripe says happened.
- **Pro (dunning):** OFFLINE dunning rides the existing grace-expiry beat
  task. Only added side effect is the notification dispatch; transition
  logic is unchanged.
- **Pro (audit):** Free row-level history for every model. Querying via
  `Subscription.history.all()` (or in admin) needs no awareness in service
  code.
- **Con (dunning):** We don't control retry timing. An org that wants
  "retry every Friday for a month" cannot have it without a custom Stripe
  retry rule on their Connect account.
- **Con (dunning):** Notification dispatch idempotency is gated in code, not
  by a delivery log. Tests cover the gates; a future incident affecting
  webhook re-delivery would be diagnosed by re-running the test suite,
  not by a delivery-receipt table.
- **Con (audit):** `historical_*` tables grow forever. Retention policy /
  pruning is deferred until size becomes a real cost (out of scope per
  Phase 4 spec §13).
- **Con (audit):** Reading a row's history during an active transaction
  requires care — concurrent saves are visible. Used only from admin and
  ad-hoc queries, not from request-path code.
