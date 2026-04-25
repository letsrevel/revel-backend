# User-initiated ticket cancellation & refunds — design

**Issue:** #370
**Status:** Design approved, ready for implementation plan
**Branch:** `feature/370-user-ticket-cancellation`
**Date:** 2026-04-23

---

## 1. Summary

Let ticket holders cancel their own tickets (free, offline/at-the-door, and online/Stripe), with automatic partial refunds for online tickets governed by a per-tier, organizer-configured policy snapshotted at purchase time.

## 2. Scope

### In scope (v1)
- Per-tier opt-in to user cancellation.
- Per-tier refund policy (tiered % by hours-before-event + flat fee).
- Automatic Stripe refunds for online tiers, evaluated against the **snapshot** stored on each ticket at purchase.
- Batch purchases: cancel one of N tickets in a batch and refund exactly that one Payment.
- Cancellation for offline / at-the-door / free tiers (seat freed, organizer handles any money owed out-of-band).
- Audit fields on `Ticket`: who, when, why, source.
- Guards: `CHECKED_IN`, event started, already `CANCELLED`, past deadline, not owner, not permitted.
- **Pre-existing bug fixes bundled in** (see §12).

### Out of scope (v1)
- Frontend implementation (separate PR).
- Returning consumed discount-code usage on cancellation.
- Reverting `EventRSVP` state (ticket flow and RSVP flow are mutually exclusive via `Event.requires_ticket`).
- Organizer-visible refund analytics dashboard.
- Organizer-initiated online refund via our API (today's Stripe-dashboard path remains; we improve its safety — see §7).
- `SYSTEM` cancellation source: dropped, no v1 use case.
- `currency` field on `RefundPolicy`: dropped, inherited from tier.

---

## 3. Key architectural findings (drive the design)

### 3.1 Payment is OneToOne with Ticket, not 1:N
`events/models/ticket.py:716` — `Payment.ticket = OneToOneField(Ticket)`. A "batch purchase" creates **one Payment per Ticket** sharing the same `stripe_session_id` / `stripe_payment_intent_id` (`events/service/stripe_service.py:473–498`, one row per `ticket in tickets`, `amount=effective_price` per-ticket).

**Consequences:**
- No `PaymentRefund` table needed. Each `Payment` is already the refundable unit. Refund fields live on `Payment` directly.
- No per-ticket division in the refund calculation. `P_ticket = payment.amount`.
- "Partial refund of a batch" = refund one `Payment` row in full (at whatever % the policy yields).

### 3.2 Pre-existing webhook bug for batch partial refunds
`events/service/stripe_webhooks.py:173–226` matches on `stripe_payment_intent_id` and refunds **all** payments sharing the intent. For a batch (e.g. 4 tickets, 1 intent), a partial Stripe refund ($100 of $400) currently cascades to all 4 tickets.

User-initiated cancellation of one ticket in a batch would trigger exactly this: we'd call Stripe with `amount = one ticket's share`, and today's webhook would cancel all four. **Fixing this is required, not optional.**

The fix: match refunds to specific Payments by `stripe_refund_id` (captured on the Payment when our API initiates the refund, set via `metadata["ticket_id"]` if it arrives fresh from a webhook).

### 3.3 `_send_refund_notifications` hardcodes `payment.amount`
`notifications/signals/payment.py:118` — always uses the full payment amount. Must read `payment.refund_amount` when set.

### 3.4 `mark_ticket_refunded` admin endpoint silently skips notifications
`events/controllers/event_admin/tickets.py:281–282` sets `ticket.status = CANCELLED` without capturing `_original_ticket_status`, so the ticket signal handler sees no transition. The sibling `cancel_ticket` (line 325) does it correctly. Bundled fix.

### 3.5 `handle_payment_intent_canceled` (expired PENDING → CANCELLED)
`events/service/stripe_webhooks.py:249–307` cancels tickets whose checkout never completed. These aren't "cancellation events" in the audit sense — the ticket was never really purchased. We do **not** set `cancelled_*` audit fields on this path. Confirms dropping `SYSTEM` source.

---

## 4. Data model changes

### 4.1 `TicketTier` — new fields

| Field | Type | Notes |
|---|---|---|
| `allow_user_cancellation` | `BooleanField`, default `False` | Master opt-in. When `False`, other fields ignored. |
| `cancellation_deadline_hours` | `PositiveIntegerField`, nullable | User can cancel up to N hours before `event.starts_at`. `None` = deadline is event start. |
| `refund_policy` | `JSONField`, nullable | Serialized `RefundPolicy`. Validated at API layer via Pydantic. `None` = cancellation allowed, zero refund. |

### 4.2 `Ticket` — new fields

| Field | Type | Notes |
|---|---|---|
| `refund_policy_snapshot` | `JSONField`, nullable | Copy of `tier.refund_policy` at purchase time. Immutable after creation. |
| `cancelled_at` | `DateTimeField`, nullable | Set only on user/organizer/dashboard cancellation. Not set for expired PENDING. |
| `cancelled_by` | `FK(RevelUser, on_delete=SET_NULL)`, nullable | |
| `cancellation_source` | `CharField(choices)`, blank | `USER`, `ORGANIZER`, `STRIPE_DASHBOARD`. No `SYSTEM`. |
| `cancellation_reason` | `CharField(max_length=500)`, blank | Free-text reason, surfaced to organizer. |

### 4.3 `Payment` — new fields (replaces `PaymentRefund` model proposed in issue)

Because `Payment` is already 1:1 with `Ticket`, a single row represents one refundable unit. No separate history table needed for v1.

| Field | Type | Notes |
|---|---|---|
| `refund_amount` | `DecimalField(max_digits=10, decimal_places=2)`, nullable | Actual amount refunded (after policy + flat fee). |
| `refund_status` | `CharField(choices)`, nullable | `PENDING`, `SUCCEEDED`, `FAILED`. Tracks Stripe refund lifecycle independently of `Payment.status`. |
| `stripe_refund_id` | `CharField(max_length=255)`, blank, `db_index=True` | Matches webhook refund objects to this payment. |
| `refund_failure_reason` | `TextField`, blank | Stripe error payload for failed refunds. |
| `refunded_at` | `DateTimeField`, nullable | Confirmed refund time (set when webhook flips `refund_status=SUCCEEDED`). |

`Payment.status=REFUNDED` continues to mean "Stripe confirmed the refund arrived" and is set by the webhook. The new fields track the refund attempt itself.

**Retry semantics:** on a failed refund, `refund_status=FAILED` and `refund_failure_reason` captured, the enclosing transaction rolls back (ticket stays `ACTIVE`). A retry reuses the same `Payment` row (overwrites the failure fields). Stripe's idempotency key (`f"refund:{ticket.id}"`) prevents double-charging. No multi-row refund history in v1 — the Stripe dashboard and our structured logs are the audit trail.

### 4.4 `RefundPolicy` Pydantic schema (API layer, not DB)

```python
class RefundPolicyTier(Schema):
    hours_before_event: int = Field(ge=0)
    refund_percentage: Decimal = Field(ge=0, le=100, max_digits=5, decimal_places=2)

class RefundPolicy(Schema):
    tiers: list[RefundPolicyTier]           # strictly descending hours, non-increasing %
    flat_fee: Decimal = Field(default=Decimal("0"), ge=0, max_digits=10, decimal_places=2)

    @model_validator(mode="after")
    def _validate_monotonic(self) -> "RefundPolicy":
        # hours_before_event strictly descending
        # refund_percentage monotonically non-increasing
        ...
```

Validated on tier create/update AND when a snapshot is loaded from the DB (defense against historical data drift).

### 4.5 Enums

```python
class CancellationSource(models.TextChoices):
    USER = "user"
    ORGANIZER = "organizer"
    STRIPE_DASHBOARD = "stripe_dashboard"

class CancellationBlockReason(models.TextChoices):
    ALREADY_CANCELLED = "already_cancelled"
    CHECKED_IN = "checked_in"
    EVENT_STARTED = "event_started"
    NOT_PERMITTED = "not_permitted"   # tier.allow_user_cancellation = False
    PAST_DEADLINE = "past_deadline"
    NOT_OWNER = "not_owner"           # caller ≠ ticket.user
```

`CancellationBlockReason` is a stable identifier used by both the preview and the cancel endpoint so the frontend can render i18n-localized copy. It lives in `events.models.ticket` alongside `TicketStatus`.

---

## 5. Refund calculation

Pure, stateless function operating on a `Ticket` + `now`.

```python
@dataclass(frozen=True)
class RefundQuote:
    can_cancel: bool
    reason: CancellationBlockReason | None
    refund_amount: Decimal         # 0 if offline/free/no-policy/past-tier/can_cancel=False
    currency: str
    deadline: datetime | None      # absolute moment cancellation is no longer allowed

def quote_cancellation(ticket: Ticket, now: datetime) -> RefundQuote: ...
```

**Algorithm (online ticket, `payment.amount = P`):**

1. Guard checks → return `can_cancel=False` with matching `reason`:
   - `ticket.status == CANCELLED` → `ALREADY_CANCELLED`
   - `ticket.status == CHECKED_IN` → `CHECKED_IN`
   - `now >= event.starts_at` → `EVENT_STARTED`
   - `tier.allow_user_cancellation is False` → `NOT_PERMITTED`
   - `cancellation_deadline_hours` set and `now > event.starts_at − N hours` → `PAST_DEADLINE`
   - (`NOT_OWNER` enforced in the controller, not here)
2. If `ticket.refund_policy_snapshot is None` → `refund_amount = 0`.
3. `hours_remaining = (event.starts_at − now).total_seconds() / 3600`.
4. Walk `policy.tiers` in declared (descending) order. First tier where `hours_remaining >= tier.hours_before_event` matches. None match → `refund_amount = 0`.
5. `base_refund = P * (tier.refund_percentage / Decimal(100))`.
6. `final_refund = max(Decimal("0"), (base_refund − policy.flat_fee).quantize(Decimal("0.01"), ROUND_HALF_EVEN))`.
7. For offline / free / at-the-door: `refund_amount = 0` regardless (no Stripe, no refund path).

**Currency:** `ticket.payment.currency` (online) or `tier.currency` (offline/free).

**Deadline:** `event.starts_at − timedelta(hours=cancellation_deadline_hours)` if set, else `event.starts_at`.

**No division, no rounding remainder** — `P` is already per-ticket (3.1).

**Snapshot authority:** always uses `ticket.refund_policy_snapshot`, never `tier.refund_policy`. A policy edit after purchase does not retroactively change refund amounts.

---

## 6. API surface

All paths are relative to the existing `/events` prefix. New endpoints authenticated via `I18nJWTAuth`.

### 6.1 GET `/events/tickets/{ticket_id}/cancellation-preview`

Powers UI decision state and the timeline curve.

```python
class RefundWindow(Schema):
    refund_percentage: Decimal
    refund_amount: Decimal           # after flat fee, floored at 0
    effective_until: AwareDatetime   # latest instant this rate applies

class CancellationPreviewSchema(Schema):
    can_cancel: bool
    reason: CancellationBlockReason | None = None
    refund_amount: Decimal           # if cancelled NOW (0 when can_cancel=False)
    currency: str
    deadline: AwareDatetime | None
    flat_fee: Decimal
    payment_method: TicketTier.PaymentMethod   # for "refund" vs "cancel" UI copy
    windows: list[RefundWindow]      # full curve; [] if no policy
    policy_snapshot: RefundPolicy | None
```

**Windows derivation:** for each tier `T_i` in the snapshot (descending by hours), `effective_until = event.starts_at − timedelta(hours=T_i.hours_before_event)`; `refund_amount` pre-computed with flat-fee subtraction. After the last window's `effective_until`, cancellation still allowed (until `deadline`) but refund drops to 0 (implicit, not a separate window).

**Permission:** caller must be `ticket.user` (else 403 with `NOT_OWNER`). Preview returns 200 even when `can_cancel=False`, with `reason` populated.

### 6.2 POST `/events/tickets/{ticket_id}/cancel`

```python
class TicketCancellationRequest(Schema):
    reason: str | None = Field(default=None, max_length=500)

class TicketCancellationResponse(Schema):
    ticket: UserTicketSchema
    refund_amount: Decimal
    currency: str
    refund_status: Payment.RefundStatus | None   # PENDING (online) or None (offline/free)
```

**Permission:** caller must be `ticket.user`.

**Response codes:**
- `200` — cancellation succeeded.
- `409` — any `CancellationBlockReason` — body includes `{"code": "<reason>", "detail": "<localized>"}`.
- `403` — `NOT_OWNER` (request schema validation; separate from 409 business rules).
- `502` — Stripe refund failed after retries; ticket remains active.

### 6.3 Modified tier admin endpoints

`POST /event-admin/{event_id}/ticket-tier`
`PATCH /event-admin/{event_id}/ticket-tiers/{tier_id}`

Accept the three new fields. `refund_policy` validated via the Pydantic `RefundPolicy` schema — errors surface as field validation errors.

### 6.4 Modified admin cancel endpoints

`POST /event-admin/{event_id}/tickets/{ticket_id}/cancel`
`POST /event-admin/{event_id}/tickets/{ticket_id}/mark-refunded`

Both set `cancelled_at=now()`, `cancelled_by=request.user`, `cancellation_source=ORGANIZER`. `mark_ticket_refunded` additionally captures `_original_ticket_status` (bundled fix for 3.4).

No request-schema change — `cancellation_reason` is optional on these existing endpoints (added to request body as optional field).

---

## 7. State machine & webhook idempotency

### 7.1 API-initiated cancel — online ticket, refund > 0

```
BEGIN TRANSACTION
  quote = quote_cancellation(ticket, now)
  if not quote.can_cancel: raise HttpError(409, quote.reason)

  refund = stripe.Refund.create(
      payment_intent=payment.stripe_payment_intent_id,
      amount=int(quote.refund_amount * 100),
      metadata={"ticket_id": str(ticket.id), "user_initiated": "true"},
      idempotency_key=f"refund:{ticket.id}",
  )

  # Update Payment (status stays SUCCEEDED until webhook)
  payment.refund_amount = quote.refund_amount
  payment.stripe_refund_id = refund.id
  payment.refund_status = Payment.RefundStatus.PENDING
  payment.save(update_fields=[...])

  # Update Ticket (atomic with refund record)
  ticket._original_ticket_status = ticket.status
  ticket._refund_amount = f"{quote.refund_amount} {quote.currency}"
  ticket.status = Ticket.TicketStatus.CANCELLED
  ticket.cancelled_at = now
  ticket.cancelled_by = request.user
  ticket.cancellation_source = CancellationSource.USER
  ticket.cancellation_reason = payload.reason or ""
  ticket.save(update_fields=[...])

  # Restore inventory (existing F-expression pattern)
  TicketTier.objects.filter(pk=ticket.tier_id).update(
      quantity_sold=F("quantity_sold") - 1
  )
COMMIT
```

On Stripe API failure: exception propagates → transaction rolls back → ticket stays `ACTIVE`, `quantity_sold` unchanged, no `Payment` mutation. User gets `502` and can retry.

Webhook arriving later: matches by `stripe_refund_id` (§7.4), flips `Payment.refund_status=SUCCEEDED`, `Payment.status=REFUNDED`, sets `refunded_at`. Does **not** re-cancel the ticket (already CANCELLED).

Ticket post_save signal fires `TICKET_CANCELLED` (from the API transaction commit). Payment post_save signal fires `TICKET_REFUNDED` (from the webhook commit). Two distinct notifications, both intended per issue spec.

### 7.2 API-initiated cancel — refund = 0 (policy yields 0), offline, free, at-the-door

No Stripe call, no `Payment` mutation (for offline/free there is no Payment, or it is offline-only). Ticket updated + `quantity_sold` decremented as above. Only `TICKET_CANCELLED` fires (no refund notification).

### 7.3 Idempotency

- `stripe.Refund.create` called with `idempotency_key=f"refund:{ticket.id}"` — a duplicate POST `/cancel` will either hit our 409 `ALREADY_CANCELLED` guard or, if the first call never flipped our DB (rare crash mid-transaction), the retried Stripe call returns the existing refund.
- Webhook redelivery: already-set `Payment.refund_status=SUCCEEDED` short-circuits the update (§7.4).

### 7.4 Webhook refactor — `handle_charge_refunded`

```python
def handle_charge_refunded(self, event):
    charge = event.data.object
    payment_intent_id = charge["payment_intent"]

    for refund in charge.get("refunds", {}).get("data", []):
        payment = self._match_refund_to_payment(refund, payment_intent_id)
        if payment is None:
            logger.warning("stripe_refund_no_match", refund_id=refund["id"], ...)
            continue
        self._apply_refund_to_payment(payment, refund, event)
```

**Matching strategy (`_match_refund_to_payment`):**

1. **Existing match:** `Payment.objects.filter(stripe_refund_id=refund["id"]).first()` → API-initiated, already has the row; just confirm it.
2. **Metadata match:** `refund["metadata"].get("ticket_id")` → `Payment.objects.filter(ticket_id=...).first()`.
3. **Exact-amount unambiguous match:** among payments for this intent with `refund_status IS NULL`, if exactly one has `amount == refund.amount`, use it.
4. **Full-intent match:** `refund.amount == sum(p.amount for p in unrefunded payments)` → treat as full-remaining-batch refund, process all.
5. **Otherwise:** log `stripe_refund_ambiguous_match` with intent id, refund id, amount, and candidate payment ids. Do not mutate anything. Organizer reconciles manually.

**`_apply_refund_to_payment`:**

```python
if payment.refund_status == Payment.RefundStatus.SUCCEEDED:
    return  # idempotent

payment.stripe_refund_id = refund["id"]
payment.refund_amount = Decimal(refund["amount"]) / 100
payment.refund_status = Payment.RefundStatus.SUCCEEDED
payment.refunded_at = now
payment.status = Payment.PaymentStatus.REFUNDED
payment.raw_response = dict(event)
payment.save(update_fields=[...])

ticket = payment.ticket
if ticket.status != Ticket.TicketStatus.CANCELLED:
    # Dashboard-initiated path: API flow didn't run
    ticket._original_ticket_status = ticket.status
    ticket._refund_amount = f"{payment.refund_amount} {payment.currency}"
    ticket.status = Ticket.TicketStatus.CANCELLED
    ticket.cancelled_at = now
    ticket.cancellation_source = CancellationSource.STRIPE_DASHBOARD
    ticket.save(update_fields=[...])
    TicketTier.objects.filter(pk=ticket.tier_id).update(
        quantity_sold=F("quantity_sold") - 1
    )
# Credit note task triggered on transaction.on_commit, as today
```

`Payment.refund_amount` used by notifications (bundled fix for 3.3).

### 7.5 Expired PENDING (`handle_payment_intent_canceled`) — unchanged
No audit fields. The ticket never transitioned out of PENDING, so it was never "cancelled" in the business sense.

---

## 8. Service layer

New file: `events/service/cancellation_service.py`.

**Public API:**

```python
def quote_cancellation(ticket: Ticket, now: datetime) -> RefundQuote:
    """Pure, stateless. Powers preview and cancel paths alike."""

def build_cancellation_preview(ticket: Ticket, now: datetime) -> CancellationPreview:
    """Wraps quote_cancellation with the windows array for UI timelines."""

def cancel_ticket_by_user(
    ticket: Ticket,
    user: RevelUser,
    reason: str,
    now: datetime,
) -> CancellationResult:
    """End-to-end flow. Validates ownership (raises on mismatch),
    calls quote_cancellation, executes Stripe refund + local mutations
    in one atomic transaction. Returns the refund amount for the response."""
```

Pattern: function-based (stateless operations, no shared request context beyond what the params carry). Follows `batch_ticket_service.py` split precedent where `create_batch()` lives in a class but standalone operations are module-level.

Controller is thin — validates auth, extracts params, calls `cancel_ticket_by_user`, maps exceptions to HTTP responses.

**RefundPolicy validation helper** in `events/utils/` (models must not import from services; validation logic is pure and reusable):

```python
def validate_refund_policy(data: dict | None) -> RefundPolicy | None:
    """Raises pydantic.ValidationError on malformed input. Used by tier admin endpoints."""
```

---

## 9. Notifications

### 9.1 Context additions

`TicketCancelledContext` gains:
- `cancellation_source: CancellationSource`
- `cancellation_reason: str`

`TicketRefundedContext` already has `refund_amount`; we update the **source** of that value to `payment.refund_amount` (fix for 3.3) with a fallback to `payment.amount` when `refund_amount is None` for legacy data.

### 9.2 Template updates
Email, in-app, and Telegram templates for `TICKET_CANCELLED` branch on `cancellation_source`:
- `USER` → "You cancelled your ticket."
- `ORGANIZER` → "Your ticket was cancelled by the organizer."
- `STRIPE_DASHBOARD` → "Your ticket was cancelled and refunded."

Staff-facing variants include `ticket_holder_name` / `email` and the `cancellation_reason`.

### 9.3 Signal wiring — unchanged
- Ticket post_save → `_send_ticket_cancelled_notifications` (`TICKET_CANCELLED`).
- Payment post_save → `_send_refund_notifications` (`TICKET_REFUNDED`).
- Both fire via `transaction.on_commit`, so they run only after the DB transaction succeeds.

No new signal handlers. The refactored webhook and new service both set the pre-save `_original_ticket_status` attribute to drive the existing transition-detection logic.

---

## 10. Guards (controller-level)

| HTTP | Condition | Body |
|---|---|---|
| 403 | caller ≠ `ticket.user` | `{code: "not_owner"}` |
| 409 | `ticket.status == CANCELLED` | `{code: "already_cancelled"}` |
| 409 | `ticket.status == CHECKED_IN` | `{code: "checked_in"}` |
| 409 | `now >= event.starts_at` | `{code: "event_started"}` |
| 409 | `tier.allow_user_cancellation is False` | `{code: "not_permitted"}` |
| 409 | past cancellation deadline | `{code: "past_deadline"}` |
| 502 | Stripe refund failed | `{code: "refund_failed", detail: "..."}` — user-safe message, Stripe error in logs |

All codes stable strings (enum values) for frontend i18n.

---

## 11. Migrations

One migration in `events` app (next number `0068`).

- Add fields to `TicketTier`, `Ticket`, `Payment`.
- Existing rows: `allow_user_cancellation=False`, `refund_policy=None`, all audit fields null/blank. **No behavior change for existing tiers or tickets.**
- No data migration needed — the snapshot is populated only at new-ticket creation time going forward.

Existing tickets purchased before migration have `refund_policy_snapshot=None` → cancellation path (when eventually permitted) yields refund 0, but organizer-initiated cancel continues to work unchanged.

---

## 12. Bundled pre-existing fixes

These are required for the feature to work correctly and are small; bundling avoids two PRs touching the same files.

1. **`handle_charge_refunded` refactor** (§7.4) — match refunds per-payment by `stripe_refund_id` / metadata / amount. Fixes batch partial-refund cascade.
2. **`_send_refund_notifications`** (`notifications/signals/payment.py:118`) — use `payment.refund_amount` when set, fall back to `payment.amount`.
3. **`mark_ticket_refunded`** (`events/controllers/event_admin/tickets.py:281`) — capture `_original_ticket_status` before setting `CANCELLED` so notifications fire.
4. **Admin `cancel_ticket` / `mark_ticket_refunded`** — populate new `cancelled_*` audit fields with `cancellation_source=ORGANIZER`.

---

## 13. Batch purchase — ticket-level propagation at purchase

`events/service/stripe_service.py:473–498` and `events/service/batch_ticket_service.py` bulk-create tickets and payments. At creation, each `Ticket` stores the current `tier.refund_policy` as its `refund_policy_snapshot` (single shared snapshot value for the batch; each ticket gets its own copy). No per-ticket variance — same tier means same policy at purchase time.

---

## 14. Discount codes

Unchanged. On cancellation, the discount code's `times_used` is **not** decremented — codes are permanently consumed. Documented in endpoint docstring and user-facing confirmation copy.

---

## 15. Recurring events / templates

Tier-level cancellation config (`allow_user_cancellation`, `cancellation_deadline_hours`, `refund_policy`) propagates from template tiers to generated instance tiers exactly like other tier fields. User-specific state on tickets (`refund_policy_snapshot`, `cancelled_*`) does not propagate (it's ticket-scoped).

---

## 16. Testing plan

### Unit — `RefundPolicy` validator
- Strictly descending `hours_before_event`.
- Monotonically non-increasing `refund_percentage`.
- Percentage range (0–100).
- Flat fee non-negative.
- Valid minimal policy (single tier).

### Unit — `quote_cancellation`
- Each tier branch hits (first tier, middle tier, last tier, no tier).
- `flat_fee > base_refund` → 0, not negative.
- Deadline set vs. None.
- `refund_policy_snapshot=None` → cancellation allowed, 0 refund.
- Each block reason returns matching enum + `can_cancel=False`.
- Offline / free / at-the-door tiers always yield 0 refund_amount.
- Snapshot authority: mutate `tier.refund_policy` after purchase → quote uses snapshot.

### Unit — window derivation
- N-tier policy produces N windows with correct `effective_until` timestamps.
- Empty policy → empty windows.
- Ticket already ineligible (past deadline) → still returns windows for UI completeness, `can_cancel=False`.

### Integration — `POST /cancel`
- Free ticket happy path.
- Offline ticket happy path.
- Online ticket with refund > 0 (mocked Stripe): verify `stripe.Refund.create` call shape + idempotency key.
- Online ticket with refund == 0 (past all tiers): no Stripe call, ticket cancelled.
- Each 4xx block reason.
- Stripe refund failure → transaction rollback (ticket still ACTIVE, no Payment mutation, `quantity_sold` unchanged).
- Idempotent duplicate POST: second call returns 409 `ALREADY_CANCELLED`.

### Integration — `GET /cancellation-preview`
- Returns correct windows for 3-tier policy.
- `can_cancel=False` cases return `reason` + `refund_amount=0` + `windows=[]`.
- Unauthenticated / wrong user → 403.

### Integration — webhook refactor
- API-initiated refund → webhook arrives → `stripe_refund_id` match → `Payment.refund_status=SUCCEEDED`, `Payment.status=REFUNDED`, ticket already cancelled (no re-cancel).
- Dashboard-initiated full-batch refund → matches via "full-intent" branch → all tickets cancelled, `cancellation_source=STRIPE_DASHBOARD`.
- Dashboard-initiated single-ticket refund via metadata → exact match on ticket_id → only that one cancelled.
- Dashboard-initiated exact-amount unambiguous match → correct single payment updated.
- Ambiguous partial refund → logs `stripe_refund_ambiguous_match`, no DB mutation.
- Duplicate webhook delivery (already SUCCEEDED) → no-op.

### Integration — batch partial cancellation
- Buy 4 tickets in one Stripe intent. Cancel 1 → only that Payment + Ticket affected, other 3 still ACTIVE.
- Cancel remaining 3 individually → sum of `Payment.refund_amount` across batch ≤ original intent total.

### Integration — admin audit fields
- Admin `cancel_ticket` populates `cancelled_at`, `cancelled_by`, `cancellation_source=ORGANIZER`.
- Admin `mark_ticket_refunded` populates audit fields **and** fires `TICKET_CANCELLED` notification (regression test for bundled fix 3.4).

### Integration — notifications
- User-initiated cancellation of online ticket sends one `TICKET_CANCELLED` (at cancel time) + one `TICKET_REFUNDED` (at webhook time), both with `cancellation_source=USER`.
- `TICKET_REFUNDED` notification reports `payment.refund_amount`, not `payment.amount` (regression test for 3.3).

---

## 17. Follow-ups (future issues, not this PR)

- Frontend cancellation dialog with live refund preview.
- Organizer-visible refund analytics dashboard.
- Organizer-initiated online refund via Revel API (currently only via Stripe dashboard).
- Admin "match Stripe refund to ticket" tool for resolving `stripe_refund_ambiguous_match` warnings.
- Return consumed discount-code usage on cancellation (if organizers request).

---

## 18. Open questions

None as of this writing — all design-level decisions resolved during brainstorming.

## 19. Risks

- **Webhook refactor is the highest-risk piece.** The fix is required (otherwise the feature cascades incorrectly), but it touches a code path that handles real-money state. Test coverage must exercise all five matching branches + the no-match safety case before merge.
- **Snapshot drift:** if we ever load a snapshot whose shape has changed (future policy-schema evolution), the Pydantic validator catches it and we need a fallback policy. Out of scope for v1 — no schema versioning yet — but worth flagging for whoever adds v2 policy fields.
- **Stripe refund latency:** `stripe.Refund.create` is synchronous and can take seconds under load. Cancel endpoint is inside a DB transaction; a slow Stripe call holds the row lock. Acceptable at v1 volume; revisit if we see lock contention.
