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
   read. It applies to ONLINE only; OFFLINE keeps the prorated amount. Stripe prorates when the
   session completes, not when it is created, so the decision is taken at the session's latest
   completion time (`expires_at`, see 6): the stub is free if it would be free then, and a session
   that could complete at or after the boundary is free by construction.
4. **The first payment records the full price it stood in for.** The prorated first invoice
   (`billing_reason=subscription_create`, one proration line, period join to boundary) is stored
   with `MembershipPayment.full_period_amount` set, the ONLINE twin of what OFFLINE records when
   staff take a first payment mid-period. The figure is resolved from the line's Stripe Price id
   (the plan's own price id gives `plan.price`; any other is retrieved on the organization's
   account), never from the line's `unit_amount_decimal` (0 or prorated on stub and zero lines) and
   not from the mutable `plan.price` alone. The `plan.price` shortcut applies only when the line's
   Price id equals the plan's. The stamp is best-effort: if the Price cannot be resolved (no Price
   id, Stripe error, no integer `unit_amount`) it falls back to `plan.price`, almost always right
   where the stub amount (possibly 0) never is, and is logged at WARNING with the subscription,
   invoice and price ids. A member who paid is never stranded over it:
   the webhook still links, activates and records the payment. The stub decision is re-taken
   against the locked subscription's plan (the Stripe fetch stays outside the lock), and the fetched
   price only counts if it is still the stub line's (a dropped stamp is logged at WARNING). It is stamped once: a redelivery never rewrites it. MRR, renewal reminders and
   price-migration notices read `Coalesce(full_period_amount, amount)` through one helper, so a
   stub never anchors a quote. A first invoice is a stub when it carries a proration line, or when its
   `total` and `subtotal` are both zero; `amount_paid` 0 alone is not enough, since a customer
   credit balance or a coupon also leaves a full period paid with nothing. A first invoice with
   neither is a full period joined on a boundary and is not stamped; neither is a `subscription_update` proration. A proration-only
   invoice carries no recurring line, so for this first invoice the period comes from the
   proration line itself (it equals the Subscription item's period); without that the row would
   stay without a period whenever `customer.subscription.*` events beat `checkout.session.completed`
   and were dropped. A free stub is the same invoice with zero total, or no invoice at all:
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
   hour can therefore still complete after the boundary; such a session is a free stub
   (`proration_behavior="none"`), so a late completion just pays the first full period from the
   anchor. Because the free-stub decision covers the latest completion time, the free-stub window is
   bounded by the session expiry (`PAYMENT_DEFAULT_EXPIRY_MINUTES`). Stripe documents that a
   session paid after its anchor is charged the full period from the anchor
   ([Set the billing cycle date](https://docs.stripe.com/payments/checkout/billing-cycle)). A probe
   in Stripe test mode confirmed it on 2026-11-22 (API version `2026-03-25.dahlia`, hosted Checkout,
   monthly price, anchor five minutes after creation, completion two to four minutes after the
   anchor). `proration_behavior="none"` and `"create_prorations"` gave the same result:
    - The page shows the stub total (0) until the member submits. Stripe refuses that first
      submit, changes the session total to the full price, and asks the member to submit again.
      The member thus sees the full price before the payment.
    - The Subscription starts at the anchor (`start_date` = anchor), not at the completion time.
      Its first period is anchor to anchor plus one interval, and `billing_cycle_anchor` moves
      forward by one interval, so the next renewal is on the next boundary.
    - The session completes with `payment_status="paid"` and an invoice, also for
      `proration_behavior="none"`. The invoice (`billing_reason=subscription_create`) is paid at
      once for the full price. It has one line for the full period, and Stripe flags that line as a
      proration. `is_calendar_first_stub` is therefore true for this invoice: the ledger row gets
      the full period and a `full_period_amount` equal to the amount paid.

   The first probe ran on a platform account, without Connect and without an application fee.
   A second probe ran on 2026-11-22 on a Stripe Standard connected account (test mode, same API
   version, the Checkout Session created with the connected account and `application_fee_percent=4.5`,
   a price of 30.00 EUR a month, anchor about three minutes after creation, both
   `proration_behavior="none"` and `"create_prorations"`). Results:
    - The page was open before the anchor. The first submit after the anchor was refused: the page
      changed the total from 0.00 to 30.00 and showed "Your order has been updated. Please review the
      updated total and submit payment again." The next submit paid. The probe made that next submit on a
      page loaded again after the refusal, because the in-page second click missed the moved button.
    - Both variants gave the same result. The session was `complete` and `paid`. The first invoice
      (`billing_reason=subscription_create`) had total, subtotal and `amount_paid` of 30.00 EUR. It had one
      line of 30.00 EUR, flagged as a proration under `parent.subscription_item_details.proration`, with the
      Price id under `pricing.price_details.price`. The line period was the anchor to one month later.
    - The application fee was 1.35 EUR. This is 4.5 percent of 30.00, the full amount paid. The fee is on the
      charge of the invoice's PaymentIntent (`latest_charge.application_fee`). The PaymentIntent has
      `application_fee_amount=135`.
    - The Subscription had `start_date` equal to the anchor and `billing_cycle_anchor` one month after it.
      The item period was the anchor to one month later. The next renewal is therefore on the next boundary.
      `application_fee_percent` stayed 4.5.
    - From the code, `record_stripe_payment_from_invoice` records one SUCCEEDED row with amount 30.00, a
      platform fee of 1.35 split into net and VAT, and the period anchor to one month later. The row gets
      `full_period_amount` of 30.00. This was not run against the captured payload.

   Not proved: a payment with a real 3-D Secure challenge, a session in a currency other than EUR, a
   different Connect account type, and the webhook handler run on the captured payload. A session still open past the boundary it was
   minted for is treated as stale: the next subscribe expires it and mints a fresh one, so a
   member is never sent back to a payable page with a stale anchor. The `sub-checkout:{pk}`
   idempotency key is only reused for the same create call, never across that replacement (the
   stale PENDING row is cleared and a new row, hence a new key, is created).
7. **The next charge is stated before payment, and short periods are reminded too.** A short or free
   stub can be followed by the full charge within hours. The Stripe Checkout page shows the
   upcoming charge, and the revival checkout-link notice (`SUBSCRIPTION_REVIVAL_CHECKOUT`) states
   the next charge (the plan price) and its date, both known before the member pays. Post-payment
   notices (`MEMBERSHIP_GRANTED`, `SUBSCRIPTION_RENEWAL_SUCCEEDED`) carry no next-charge line. The
   renewal reminder beat also reminds any subscription whose current period ends inside the
   reminder window and has not been reminded yet, so a stub shorter than `REMINDER_DAYS` is
   reminded before its first full charge **if a daily run falls inside it**; a stub that starts and
   ends between two runs (under a day) relies on the Checkout page. `renewal_reminded_for` records
   the period end already reminded (compared by date), one reminder per period; migration `0148`
   backfills it so the deploy-day run doesn't re-remind periods the old exact-day rule covered.

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
