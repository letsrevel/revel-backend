# ADR-0011: Hybrid OFFLINE/ONLINE Membership Subscriptions

## Status

Accepted

## Context

Revel needed recurring membership revenue alongside one-off ticket sales. Two
patterns were on the table:

1. **Stripe-only**: All subscriptions are Stripe Subscriptions on the org's
   Connect account. No platform-side state machine; we just mirror Stripe.
2. **Hybrid OFFLINE/ONLINE**: One local `MembershipSubscription` state machine
   that can be backed by Stripe (ONLINE) **or** managed entirely by staff
   (OFFLINE), with cash, bank transfers, or any other off-Stripe collection
   recorded via `MembershipPayment` rows.

A pure Stripe model would have been faster to ship but locks every customer
out of platforms or fee categories Stripe doesn't support — non-profits with
cash-paying members, organizations whose members pay annually by SEPA, orgs
on Stripe-restricted geographies, and orgs not yet onboarded to Connect at
all. The OFFLINE flow also unblocked staff-managed migrations from
spreadsheets/legacy systems without a payment-method change for existing
members.

## Decision

**Option 2: hybrid.** One local state machine (`MembershipSubscription.status`
∈ {PENDING, ACTIVE, PAUSED, PAST_DUE, CANCELLED, EXPIRED}) is authoritative for
both flows. `MembershipSubscriptionPlan.payment_method` decides which side
drives transitions:

| `payment_method` | Period advancement | State authority |
|---|---|---|
| `OFFLINE` | Staff calls `record_payment` | Local service + daily Celery beat |
| `ONLINE` | Stripe `invoice.paid` webhook | Stripe (mirrored locally) |

Key consequences of the split:

- **One status enum, one state graph.** Member-facing UI, admin tooling, the
  `OrganizationMember` sync signal, and metrics all read a single `status`
  field regardless of payment_method.
- **Service-layer dispatch.** `cancel_subscription`, `pause_subscription`,
  `resume_subscription`, `change_plan` etc. check `plan.payment_method` and
  route to `subscription_stripe_service` (or the dedicated
  `subscription_stripe_plan_change` module) for ONLINE flows. Controllers do
  not branch.
- **`payment_method` is not patchable.** Switching an existing subscription
  between OFFLINE and ONLINE would require non-trivial Stripe migration with
  no obvious correct period reconciliation. The plan must be archived and a
  new plan created instead (`PlanUpdateSchema` omits the field; `change_plan`
  refuses cross-method changes).
- **Member-creation responsibility differs.** OFFLINE: `create_subscription`
  ensures `OrganizationMember` exists up front. ONLINE: the local row is
  created in PENDING; `_ensure_active_member` only runs after Stripe confirms
  the first invoice (no tier benefits before payment).

## Consequences

- **Pro:** One code path for permissions, member sync, expiry, reporting, and
  notifications. Adding a third payment method later (e.g. ACH) is a new
  branch in the service layer, not a parallel state machine.
- **Pro:** Organizations can run a mix of OFFLINE plans (e.g. cash-paying
  legacy members on a "Founders" tier) and ONLINE plans (e.g. Stripe-billed
  Monthly/Annual) on the same tier hierarchy.
- **Con:** Some operations have two implementations (cancel, pause, resume,
  change_plan, revive). The dispatch is concentrated in
  `subscription_service.py` and verified by paired tests
  (`test_subscription_service.py` + `test_subscription_stripe_service.py`).
- **Con:** ONLINE payments are never hand-recorded — `record_payment`
  accepting an ONLINE plan would create duplicates with the `invoice.paid`
  webhook. The admin record-payment endpoint refuses ONLINE subscriptions
  explicitly.
- **Con:** The duplicated `_stripe_account_kwargs` helper in
  `subscription_stripe_plan_change.py` exists to avoid an import cycle with
  `subscription_stripe_service`. Accepted as a small, contained price.
