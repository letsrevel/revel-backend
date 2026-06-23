# Stripe webhook endpoints

Steady-state reference for Revel's Stripe webhook setup. For the one-time
migration that established it, see
[stripe-hardening-rollout.md](stripe-hardening-rollout.md).

## Topology

Two Stripe webhook endpoints, both pointing at the same URL
(`https://<host>/api/stripe/webhook`), distinguished by Stripe's `connect`
flag. Stripe delivers an event to every endpoint subscribed to it; the two
endpoints never overlap because platform-account events and connected-account
events are disjoint by construction.

| Endpoint | `connect` | Subscribed events | Delivers |
|---|---|---|---|
| Platform ("Your account") | `false` | `checkout.session.completed`, `charge.refunded`, `payment_intent.canceled` | Events on `STRIPE_ACCOUNT` itself — i.e. the host-bound organization's sales |
| Connect ("Connected accounts") | `true` | same + `account.updated` | Events on organizations' connected accounts (`event.account` is set) |

Both endpoints are pinned to `STRIPE_API_VERSION` — webhook payload shapes
follow the **endpoint's** pinned version, while outbound call/response shapes
follow the `stripe.api_version` set in code. Keep them the same version; a
future bump is a deliberate two-step (outbound pin first, then re-provision
endpoints) with a compat audit in between.

The subscribed event lists live in
`events/management/commands/provision_stripe_webhooks.py` and **must stay in
sync** with the dispatch map in
`events/service/stripe_webhooks.py::StripeEventHandler.handle` — Stripe only
delivers events you subscribe to, and unmapped deliveries land as `unhandled`
rows in the event log.

## Secrets

- `STRIPE_WEBHOOK_SECRETS` (CSV, no spaces) holds one `whsec_*` per endpoint;
  `verify_webhook` tries each in order, first HMAC match wins. Order them
  most-traffic-first (Connect endpoint first in practice).
- Secrets are shown by Stripe **exactly once**, at endpoint creation (the
  provisioning command prints them). If one is lost: delete that endpoint in
  the dashboard and re-provision.
- The legacy single `STRIPE_WEBHOOK_SECRET` is only a fallback when the CSV is
  unset; steady state does not use it.

## Provisioning / rotation

```bash
# Preview
docker compose exec web python manage.py provision_stripe_webhooks \
  --url https://<host>/api/stripe/webhook --dry-run

# Create the pair (--force if endpoints already target the URL, e.g. rotation)
docker compose exec web python manage.py provision_stripe_webhooks \
  --url https://<host>/api/stripe/webhook --force
```

Rotation = provision a new pair with `--force`, add the new secrets to the CSV
*alongside* the old ones, restart, verify deliveries, then disable the old
endpoints in the dashboard and drop their secrets. The event-log dedup
(`StripeWebhookEvent.event_id` unique) makes the overlap window harmless.

!!! tip "Machine-readable output (1.64.0+)"

    Pass `--format json` to print **only** a JSON object
    (`{"platform": {"id", "secret"}, "connect": {"id", "secret"}}`) instead of the
    human-readable secrets block — for scripted self-hosted setup (e.g. `setup.sh`).
    It cannot be combined with `--dry-run`.

## Host-as-org binding

Exactly one organization may use the platform's own Stripe account:
Django admin → Organizations → select one → **"Bind to platform Stripe
account (superuser only)"**. Its checkout/refund events arrive via the
*platform* endpoint (`event.account` empty, stored as `""` in the event log).
No application fee is charged (Stripe forbids fees on own-account charges).
**"Unbind platform Stripe account"** reverses it and refuses to touch real
Connect bindings. The unique constraint on `stripe_account_id` prevents
double-binding.

## Observability

- Event log: `/admin/events/stripewebhookevent/` — every verified delivery,
  with outcome (`handled`/`unhandled`), `account`, full payload; pruned after
  `STRIPE_WEBHOOK_EVENT_RETENTION_DAYS` (default 90 — keep ≥ 30, the manual
  resend window).
- Logs: `scripts/loki_logs.py web -g stripe_webhook --since 1h` — key events:
  `stripe_webhook_duplicate` (dedup hit; expected during rotation overlap),
  `stripe_webhook_signature_failed` (no secret matched → 403; investigate if
  persistent), `stripe_webhook_unhandled_event` (subscribed-but-unmapped type).
- Stripe side: Workbench → Webhooks shows per-endpoint delivery success rates.
