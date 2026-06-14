# ADR-0012: Stripe Connect Direct Charges for Subscriptions

## Status

Accepted

## Context

ONLINE membership subscriptions had to plug into Revel's existing Stripe
Connect plumbing (used for ticket sales). Stripe Connect offers three charge
models:

1. **Destination charges** — Platform owns the Customer; funds settle to the
   connected account via a `transfer_data.destination` flag.
2. **Separate charges and transfers** — Platform charges; later issues
   `stripe.Transfer.create` to the connected account.
3. **Direct charges** — Connected account owns the Customer and the
   Subscription; the platform takes its cut via `application_fee_percent`.

Tickets already use **direct charges** on Connect — Customers live on the
org's Stripe account, Revel scopes API calls with `stripe_account=...`, and
the platform fee is taken via `application_fee_percent`. Subscriptions needed
to fit this same model so an org's Stripe dashboard, payouts, refund tooling,
and Customer Portal worked uniformly across tickets and memberships.

## Decision

**Direct charges on Connect** for subscriptions, matching the ticket flow.

Specific implications:

- **`CustomerProfile` is per-(user, organization).** Each Connect account has
  its own Customer namespace, so a single platform-wide `stripe_customer_id`
  on `RevelUser` doesn't work. The `unique_customer_per_user_org` constraint
  enforces one Customer per (user, org); `ensure_customer_profile` is the
  single create/lookup entrypoint and uses a deterministic
  `idempotency_key=cust:{user}:{org}` to prevent duplicates under concurrent
  first-time subscribes.
- **Every Stripe API call is scoped via `_stripe_account_kwargs(org)`.** It
  returns `{"stripe_account": org.stripe_account_id}` for connected orgs and
  `{}` for the platform's own account (mirroring `stripe_service` for
  tickets).
- **Customer Portal** uses
  `stripe.billing_portal.Session.create(customer=..., stripe_account=...)`.
  Members manage their saved card and download Stripe-hosted invoices on the
  org's Stripe account, not on the platform.
- **Webhook routing.** A Stripe webhook endpoint listens to *either* the
  platform's own account or connected accounts — not both. The platform's
  webhook is configured for **Connect events** so ticket and subscription
  events all flow through the same endpoint. (See
  `docs/architecture/billing-and-vat.md` for the "own events vs. connected
  accounts" caveat.)
- **Schedules and prices also live on the connected account.** The
  `SubscriptionSchedule` used for downgrades and the `stripe_product_id` /
  `stripe_price_id` pair stored on `MembershipSubscriptionPlan` are all
  scoped to the org's Connect account.

## Consequences

- **Pro:** Uniform model with ticket sales. Refunds, payouts, dispute
  handling, and Stripe Dashboard access all work the same for an org's staff.
- **Pro:** Platform fees collected automatically via `application_fee_percent`
  (set from `Organization.platform_fee_percent` and omitted when the org
  shares the platform's own Stripe account).
- **Pro:** Members get a Stripe-hosted Customer Portal scoped per
  organization — payment methods saved against one org never leak to another.
- **Con:** A new Stripe Customer is created on each Connect account the user
  subscribes to. Acceptable: members typically subscribe to a small number of
  orgs, and the per-(user, org) Customer is needed for the portal scoping
  anyway.
- **Con:** Direct charges put the connected account on the hook for chargebacks
  and Stripe's fees. This matches Connect's design and is consistent with
  ticket sales.
- **Con:** A bug or compromise in one org's Connect account cannot be
  contained by platform-level rollback — the connected account owns the
  Customer/Subscription/Schedule rows.
