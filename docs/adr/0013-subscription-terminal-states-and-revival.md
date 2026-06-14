# ADR-0013: Terminal Subscription States with Bounded In-Place Revival

## Status

Accepted

## Context

A subscription that has lapsed past its grace window — or has been cancelled
outright — needs a definite end state for several reasons:

- The partial unique index `one_active_subscription_per_user_org` (excluding
  CANCELLED/EXPIRED) lets a member create a fresh subscription cleanly,
  without UPDATE-then-INSERT race conditions.
- The status-based `OrganizationMember` sync signal needs an unambiguous
  signal that the member has lost access (CANCELLED) vs. is in a transient
  state (PAST_DUE, PENDING).
- Reporting (`subscription_reporting`) treats CANCELLED/EXPIRED as the churn
  bucket — anything that can flip back ambiguously breaks the metric.

But a strict no-revival rule creates real UX pain. A member whose card
expires three days after a payment failure is functionally the same person
who lost access to it — making them register a new subscription, lose audit
continuity, and re-pick their plan from scratch is hostile.

## Decision

**CANCELLED and EXPIRED are terminal in the state machine, with a single
bounded escape hatch: in-place revival from EXPIRED, gated by a per-org
window.**

### Terminal contract

- `MembershipSubscription.TERMINAL_STATUSES = frozenset({"cancelled",
  "expired"})`.
- `record_payment` against a terminal subscription is refused outright — no
  silent period extension. Callers must use `revive_subscription` or create a
  fresh subscription instead.
- The webhook sync path (`sync_subscription_from_stripe`) gives terminal a
  hard win over `pause_collection`: a late `customer.subscription.deleted`
  event carrying a stale pause flag does **not** un-terminalize the local
  row.
- `_apply_stripe_price_swap` short-circuits on terminal rows so a late
  webhook cannot rewrite the historical plan FK.

### Revival window

- New field `Organization.membership_subscription_revival_window_days`
  (default 30; `0` disables revival entirely for the org).
- New field `MembershipSubscription.expired_at` is stamped on every
  transition into EXPIRED (Celery beat, `customer.subscription.deleted`,
  `cancel_subscription(immediate=True)` on an already-lapsed sub). Legacy
  EXPIRED rows (pre-Phase-4) have `expired_at = NULL` and are **not
  revivable** — the absence of the timestamp is the deliberate signal.
- `revive_subscription` validates: status is EXPIRED, `expired_at` is set,
  `now - expired_at <= window`, the user has no other non-terminal sub in
  the org, and the user is not BANNED.

### Revival flow

- **OFFLINE**: caller passes an `InitialPayment`; the same transaction sets
  status to ACTIVE and records the payment. `dispatch_renewal_notification`
  is forced to `False` — the revival success itself is the member-visible
  confirmation, not a "renewal" notification.
- **ONLINE**: A cancelled Stripe Subscription cannot be reactivated. We
  create a **fresh** Stripe Subscription on the plan's *current*
  `stripe_price_id` and overwrite `stripe_subscription_id`. The previous id
  lives on in `historical_membership_subscription` (simple-history). The
  idempotency key is scoped to `expired_at` so a future revival (after
  another EXPIRED transition with a fresh timestamp) gets a distinct key.

### What revival is *not*

- **Not** a way to undo CANCELLED. CANCELLED is a deliberate operator/member
  action — revival from CANCELLED would mask intent. Members must create a
  new subscription.
- **Not** a way to reach back arbitrarily far. The window is the lever; the
  default 30 days picks the "expired card, came back next week" case
  without supporting a "lapsed for a year, surprise re-bill" footgun.

## Consequences

- **Pro:** Audit continuity. A revived subscription is the same row with
  preserved history of cancellations, refunds, plan changes, and (via
  simple-history) every prior Stripe subscription id it was bound to.
- **Pro:** The partial-unique index is preserved without per-row state
  fiddling — revival transitions EXPIRED → ACTIVE atomically under
  `select_for_update`, and the validation explicitly refuses concurrent
  non-terminal subs.
- **Pro:** Disabling revival is a single column change
  (`revival_window_days = 0`); no code path needs to be removed.
- **Con:** Two correct paths to "renew a lapsed member": revival (within
  window) vs. fresh subscription (outside window or from CANCELLED). The
  UI must distinguish them. The `SUBSCRIPTION_EXPIRED` notification carries
  a revival CTA URL only when the window applies, so members see the right
  affordance for their state.
- **Con:** Revival pricing uses the plan's *current* price, not the
  grandfathered price the member previously paid. This is intentional —
  revival is re-engagement, and locking lapsed customers to old prices
  would degrade reporting (MRR computed from current `plan.price`).
