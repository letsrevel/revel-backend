# ADR-0019: Calendar-aligned membership plans

## Status

Accepted (2026-11-18)

## Context

Membership plans billed on a rolling duration: a member who joined on 18 November paid on
18 December, 18 January and so on. Organizations that run a season or a club year want every
member on the same grid: a calendar month, or a calendar year that starts in a chosen month
(January, or September for a season). Epic #1424 scoped this; #1425 built the data model and
the OFFLINE maths, #1426 the ONLINE (Stripe) side.

Stripe was probed in test mode (API `2026-03-25.dahlia`) before deciding:

- `subscription_data.billing_cycle_anchor` plus `proration_behavior="create_prorations"` on a
  `mode=subscription` Checkout Session charges a prorated first invoice and renews on the anchor.
  A 120 EUR yearly plan joined on 18 November charged 14.33 EUR; the period ended on 1 January;
  the application fee was taken.
- A full-price first period has no native mechanism. Anchor plus `proration_behavior="none"` plus
  a one-time item is refused by Checkout; on a direct subscription the item is not invoiced until
  the boundary. The only route that worked was a trial until the boundary plus a one-time item,
  which makes Checkout say "free trial" and puts a 0 EUR trial line on the invoice.
- Anchor plus `proration_behavior="none"`: Checkout accepts it (session total 0). A direct
  subscription with it opens no invoice at all and renews on the boundary. A prorated stub below
  Stripe's minimum charge is not rejected either: a 45 minute stub of a 12 EUR monthly plan came back
  as an invoice with `total` 1 cent, `amount_due` and `amount_paid` 0, an empty `payments` list (no
  PaymentIntent) and a proration line with a null `unit_amount_decimal`.
- `Subscription.modify` cannot move an anchor to a future date (`now` or `unchanged` only).
- Anchors and Stripe's month arithmetic are UTC. A local-midnight anchor east of UTC lands on the
  last day of the previous month and then drifts with month lengths.

## Decision

1. **The boundary is 00:00 UTC on the 1st**, for ONLINE and OFFLINE alike, with no organization
   timezone. In Vienna a period ends at 01:00 or 02:00 local time on the 1st.
2. **Alignment is a plan property that cannot change.** `period_alignment`, `calendar_start_month`
   and (for calendar plans) `period_unit` are set at creation, like `payment_method` in
   [ADR-0012](0012-membership-subscriptions-offline-online-hybrid.md). Re-aligning would move
   every subscriber's boundaries. To change, archive the plan and create a new one; existing
   subscribers stay on their rolling plan.
3. **ONLINE plans prorate the first period, nothing else.** Checkout and revival sessions for a
   calendar plan pass `billing_cycle_anchor = <next boundary>` and
   `proration_behavior="create_prorations"`. No trials, no one-time line items: they are the only
   way to charge a full first period and they show members a "free trial". `FULL` first-period
   pricing is therefore refused on ONLINE plans and stays an OFFLINE option, where it only drives
   the amount staff are prompted with.
   **A tiny stub is free.** When the prorated first-period price is below Stripe's minimum charge
   for the currency (0.50 for EUR and USD, 0.30 for GBP, a small table in
   `events/utils/subscription_periods.py` with a deliberately high default for currencies not in
   it), or the next boundary is less than an hour away, the session uses
   `proration_behavior="none"`: billing starts at the boundary and the member pays nothing until
   then. Charging a few cents is either impossible (Stripe cannot collect it) or not worth a
   payment that is followed by the full charge within hours. The rule lives in one place,
   `is_free_stub`, which checkout and the quoted first-period price (zero for such a join) both
   read. It applies to ONLINE only; OFFLINE keeps the prorated amount.
4. **The first payment records the full price it stood in for.** The prorated first invoice
   (`billing_reason=subscription_create`, one proration line, period join to boundary) is stored
   with `MembershipPayment.full_period_amount` set, the ONLINE twin of what OFFLINE records when
   staff take a first payment mid-period. The figure is the line's own Stripe Price (retrieved,
   because the dahlia proration line has no `unit_amount_decimal`), not the mutable `plan.price`,
   and it is stamped once: a redelivery never rewrites it. MRR, renewal reminders and
   price-migration notices read `Coalesce(full_period_amount, amount)` through one helper, so a
   stub never anchors a quote. A first invoice with no proration line is a full period joined on a
   boundary and is not stamped; neither is a `subscription_update` proration. A proration-only
   invoice carries no recurring line, so for this first invoice the period comes from the
   proration line itself (it equals the Subscription item's period); without that the row would
   stay without a period whenever `customer.subscription.*` events beat `checkout.session.completed`
   and were dropped. A free stub is the same invoice with `amount_paid` 0, or no invoice at all:
   the ledger row (if any) has amount 0 and raises no unresolved-PaymentIntent or fee alarm, and
   `checkout.session.completed` mirrors the Subscription when the session carries no invoice.
   Notices never turn that 0 into the plan price.
5. **An ONLINE subscription may only change plan inside its own grid.** Both plan-change paths
   keep Stripe's anchor (an upgrade is a price swap, a downgrade schedules phase 2 at the old
   period end), so a plan on another grid would bill off its calendar. Allowed: rolling to
   rolling, or calendar to calendar with the same unit and start month. Anything else is refused
   (400) with a message telling the member to cancel and subscribe to the other plan. OFFLINE
   staff can switch freely: the paid period stands and the next payment snaps to the boundary.
6. **The Checkout session must not outlive its anchor.** The anchor is fixed when the session is
   created. `expires_at` is capped at one minute before the boundary and floored at 31 minutes
   after creation (Stripe's minimum is 30, plus latency). A session minted inside the last half
   hour can therefore still complete after the boundary; because that session was minted under an
   hour before the boundary, it is a free stub (`proration_behavior="none"`), so a late completion
   just pays the first full period from the anchor. What Stripe does with an anchor that has
   already passed at completion was not probed. A session still open past the boundary it was
   minted for is treated as stale: the next subscribe expires it and mints a fresh one, so a
   member is never sent back to a payable page with a stale anchor. The `sub-checkout:{pk}`
   idempotency key is only reused for the same create call, never across that replacement (the
   stale PENDING row is cleared and a new row, hence a new key, is created).
7. **Notices state the next full charge.** A short or free stub can be followed by the full charge
   within hours, so the 3 day renewal reminder cannot warn the member. The checkout-link notice
   and the first-payment confirmation therefore also state the next charge (the plan price) and
   its date (the boundary).

## Consequences

- Every calendar subscriber renews at the same instant, so each boundary brings a burst of
  `invoice.paid` webhooks and renewal reminders. Handlers are per-subscription and idempotent
  (the payment upsert keys on the Stripe invoice id), so the burst is load, not a correctness risk.
- A refund of the prorated first payment auto-cancels the subscription: the ledger row's
  `period_start` (the invoice line) equals the subscription's `current_period_start` (its item),
  which `src/events/tests/test_service/test_stripe_calendar_anchor_integration.py` checks against
  real Stripe. This also holds when the events arrived before the session completed, because the
  completion backfill now derives the period from the proration line.
- The minimum-charge table is ours, not read from Stripe: a currency missing from it uses a high
  default (a few cents of free stub beats a failed charge). Add the currency when it needs an exact figure.
- Live ONLINE subscribers cannot be moved onto a calendar plan (no future anchor change). A later
  route could cancel at period end and email a checkout link for the new plan, as staff-initiated
  revival does.
- Out of scope: a late-join cutoff, calendar periods longer than one unit, and an organization
  timezone.
- The integration canary is skipped unless Stripe test keys and `CONNECTED_TEST_STRIPE_ID` are
  configured (`pytest -m integration`). If it fails, Stripe changed a shape the flow relies on.
