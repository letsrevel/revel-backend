# ADR-0009: Referral Payout Bounded Contexts

## Status

Accepted

## Context

The referral system spans multiple domains: user identity (accounts), payment
data (events), and financial disbursement (Stripe). When implementing
`ReferralPayout` — a model that records monthly calculated earnings — we needed
to decide where models, services, and tasks should live.

Three approaches were considered:

1. **Everything in accounts**: Models, calculation service, and Celery task all
   in the `accounts` app. The service imports `Payment`/`Organization` from
   events. Simple, but the payout calculation has no conceptual relationship to
   user identity — it's a revenue computation over payment data.

2. **New `referrals` app**: Clean bounded context, but `register_user()` in
   accounts must atomically validate the referral code and create a `Referral`
   record. This forces either a cross-app import (coupling remains, just moved)
   or a signal (loses the atomic 422 rejection required by the design).

3. **Split by responsibility**: accounts owns the referral *data* (models),
   events owns the *computation* (payout calculation), and accounts owns the
   future *disbursement* (Stripe transfer of calculated payouts).

## Decision

**Option 3: split by responsibility.**

| Concern | App | Rationale |
|---------|-----|-----------|
| `ReferralCode`, `Referral`, `ReferralPayout` models | accounts | Referral identity is part of the user domain |
| Registration (validate code, create `Referral`) | accounts | Must be atomic with user creation |
| Payout calculation (aggregate `Payment` fees, create `ReferralPayout`) | events | Queries `Payment` and `Organization` — lives near the data it reads |
| Payout disbursement (Stripe transfer) | accounts (future) | Picks up `CALCULATED` payouts and processes them — owns the referral lifecycle |

### Cross-app writes

The events payout task creates `ReferralPayout` records (an accounts model).
This is the same pattern used by signals in events that create notification
records in the notifications app — a writer in one app, model in another. The
alternative (keeping the calculation in accounts) would require the accounts
service to import `Payment` and `Organization`, which is a heavier coupling for
no structural benefit.

## Consequences

- **events → accounts (write)**: The payout calculation task in events creates
  `ReferralPayout` records. This is acceptable because the model is a simple
  data store and the write is a `get_or_create` with no business rules beyond
  what the service enforces.
- **Discoverability**: Developers looking at `ReferralPayout` must check
  `events/service/referral_payout_service.py` to find where records are created.
  The model docstring points to this.
- **Future flexibility**: If payouts evolve beyond cash (vouchers, credits), the
  calculation stays in events and only the disbursement mechanism in accounts
  changes.
