# Stripe hardening rollout runbook

Operator procedure for the four-phase Stripe hardening rollout (issue
[#483](https://github.com/letsrevel/revel-backend/issues/483)): API version pinning,
webhook event log + idempotency, two-endpoint (platform + Connect) webhook setup, and
host-as-org binding.

**Golden rules**

1. **Demo first, always.** demo.letsrevel.io mirrors prod (own env, own Stripe test-mode
   endpoint). Every phase — especially the Phase 3 cutover — runs there first and is
   verified with a test purchase before touching prod.
2. **Phases are ordered.** Phase 3 (endpoint cutover) MUST NOT run before Phase 1 is
   deployed: the `StripeWebhookEvent` dedup is what makes the old/new endpoint overlap
   window harmless.
3. **Webhook secrets are shown exactly once** (at endpoint creation). Copy them
   immediately.

**Version anchors (confirmed 2026-06-10)**

| Surface | Before | After |
|---|---|---|
| Inbound webhook payloads (old endpoint pin) | `2025-07-30.basil` | `2026-03-25.dahlia` (Phase 3) |
| Outbound API responses (stripe SDK 14.3.0 built-in) | `2026-01-28.clover` | `2026-03-25.dahlia` (Phase 2) |

**Log keys quick reference** (read with the Loki CLI — see the `loki-logs` skill)

| structlog key | Meaning |
|---|---|
| `stripe_webhook_duplicate` | Redelivered event id no-opped. **Expected noise during Phase 3 overlap.** |
| `stripe_webhook_signature_failed` | No configured secret matched → 403. Investigate if persistent. |
| `stripe_webhook_secret_missing` | Only placeholder secrets configured → everything 403s. Config error. |
| `stripe_webhook_malformed_json` | Valid HMAC but unparseable body. Should never happen from Stripe. |
| `stripe_webhook_unhandled_event` | Subscribed-but-unmapped event type. Expected for the over-subscribed old endpoint until cutover. |
| `stripe_payment_success` / `stripe_refund_processed` | Handler success paths. |

Admin event log: `/admin/events/stripewebhookevent/` (read-only; filter by
`event_type`, `outcome`, `account`).

---

## Phase 1 — Hardening deploy (no behavior change)

**Preconditions:** PR 1 merged. No env changes needed — the code falls back to the
existing single `STRIPE_WEBHOOK_SECRET`.

**Steps (demo, then prod):**

1. Deploy backend as usual (`./deploy.sh update` on the target host); migrations run on
   deploy (new `StripeWebhookEvent` table + the pruning beat task).
2. Verify the migration applied:
   ```bash
   docker compose exec web python manage.py showmigrations events | tail -5   # all [X]
   ```
3. Verify fail-closed signature handling (this used to 400/500, must now 403):
   ```bash
   curl -is -X POST https://<host>/api/stripe/webhook \
     -H 'Content-Type: application/json' -d '{}' | head -1
   # expect: HTTP/2 403
   ```
4. Generate real webhook traffic — make a test purchase (demo) or wait for organic
   traffic (prod) — then confirm rows appear in `/admin/events/stripewebhookevent/`:
   - the purchase's `checkout.session.completed` row has `outcome=handled` and a
     non-empty `account` (it arrives via the Connect endpoint);
   - a burst of `charge.*` / `payment_intent.*` rows with `outcome=unhandled` is
     **expected** — the old endpoint is over-subscribed (24 types vs 4 handled). This
     noise disappears at Phase 3 cutover.
5. Check logs are clean:
   ```bash
   .venv/bin/python scripts/loki_logs.py web -g stripe_webhook --since 1h
   # expect: no stripe_webhook_signature_failed for Stripe-originated calls
   ```
6. Confirm the beat task is registered: `/admin/django_celery_beat/periodictask/` →
   "Prune Stripe webhook events", enabled, daily 04:45 UTC.

**Rollback:** redeploy the previous image. The new table is additive; leaving it in
place is harmless.

---

## Phase 2 — Pin API version to dahlia (outbound only)

**Preconditions:** Phase 1 deployed and verified. Compat audit (clover → dahlia for
Checkout Session, Charge, Refund, PaymentIntent, Account) recorded in PR 2.

**Steps (demo, then prod):**

1. Deploy PR 2. No env change needed (`STRIPE_API_VERSION` defaults to
   `2026-03-25.dahlia`); set the env var only if you need a different pin.
2. **Full payment loop on demo:** test purchase → confirm in order:
   - checkout session opens and completes (outbound `Session.create` at dahlia works);
   - `checkout.session.completed` row `outcome=handled`; ticket ACTIVE; invoice generated;
   - refund the charge from the Stripe dashboard → `charge.refunded` row
     `outcome=handled`, ticket CANCELLED, credit note generated.
3. Exercise Connect onboarding sync (outbound `Account.retrieve` at dahlia): in the org
   admin settings, trigger "verify Stripe account" for any connected org — flags should
   sync without error.
4. Watch for new errors:
   ```bash
   .venv/bin/python scripts/loki_logs.py web -l error -g stripe --since 2h
   ```

**Rollback:** set `STRIPE_API_VERSION=2026-01-28.clover` in the env and restart (no
redeploy needed), or redeploy the previous image. Inbound payloads are untouched by
this phase either way.

---
## Phase 3 — Endpoint cutover (the order-sensitive one)

**Preconditions:** Phases 1 + 2 deployed and verified **on this host**. PR 3 (the
provisioning command) deployed. Run the WHOLE procedure on demo first.

**Steps:**

1. Preview what will be created:
   ```bash
   docker compose exec web python manage.py provision_stripe_webhooks \
     --url https://<host>/api/stripe/webhook --dry-run
   ```
2. Create the two new endpoints (old one still exists → `--force`):
   ```bash
   docker compose exec web python manage.py provision_stripe_webhooks \
     --url https://<host>/api/stripe/webhook --force
   ```
   **Immediately copy both printed `whsec_*` secrets** — Stripe never shows them again.
   (If lost: delete the endpoint in the dashboard and re-run.)
3. Update the env — new secrets first, **old secret kept last** (the overlap window):
   ```
   STRIPE_WEBHOOK_SECRETS=<new_platform_whsec>,<new_connect_whsec>,<old_whsec>
   ```
4. Restart the web service (seconds of downtime is fine — Stripe retries any 5xx/timeouts).
5. Verify the overlap is working — make a test purchase (demo) or watch organic traffic
   (prod). Expect **double delivery** on Connect events: one `handled` row per logical
   event plus `stripe_webhook_duplicate` log lines as the second endpoint's copy is
   deduped. Both are healthy signals:
   ```bash
   .venv/bin/python scripts/loki_logs.py web -g stripe_webhook_duplicate --since 1h
   ```
6. Check the Stripe dashboard (Workbench → Webhooks): both new endpoints show
   successful deliveries, **zero failures**. The new endpoints are pinned
   `2026-03-25.dahlia` and subscribe only to the 4 handled event types.
7. **Soak:** demo — one verified purchase + refund is enough. Prod — leave the overlap
   for ~24h of organic traffic.
8. End the overlap, in this order:
   1. **Disable** (don't delete) the OLD endpoint in the Stripe dashboard — keeps
      instant rollback available;
   2. remove the old secret from `STRIPE_WEBHOOK_SECRETS`;
   3. restart web.
9. Final verification: next purchase produces exactly one row per event, and
   `stripe_webhook_duplicate` lines stop appearing. After a quiet week, delete the
   disabled endpoint.

**Rollback (any point):** re-enable the old endpoint in the dashboard and restore its
secret to the CSV. The multi-secret verify tolerates extra secrets, so rollback is
purely additive and instant.

---

## Phase 4 — Bind the host org

**Preconditions:** Phases 1–3 complete on this host. Decide which organization is the
host org (it must have **no** Stripe account connected yet).

**Steps (demo first with a test-mode purchase, then prod with a real card):**

1. Django admin → Organizations → select **exactly one** org → action
   **"Bind to platform Stripe account (superuser only)"**. Confirm the success message;
   the org's `stripe_account_id` now equals `STRIPE_ACCOUNT`.
2. Create a paid ticket tier on one of the org's events and buy it:
   - checkout opens (no `stripe_account` param, **no application fee** — verify the
     payment in the Stripe dashboard shows no fee);
   - the `checkout.session.completed` row appears with **empty `account`** (platform
     endpoint delivery) and `outcome=handled`;
   - ticket ACTIVE, invoice generated.
3. Refund that charge from the Stripe dashboard → `charge.refunded` (empty `account`)
   `handled`, ticket CANCELLED, credit note generated.
4. Confirm a regular Connect org still works (any organic purchase, or one test
   purchase on demo).

**Rollback:** admin action **"Unbind platform Stripe account"** (refuses to touch real
Connect bindings). Connect orgs are unaffected throughout.

---

## Post-rollout state

- `STRIPE_WEBHOOK_SECRETS` holds exactly two secrets (platform + Connect).
- `STRIPE_WEBHOOK_SECRET` (legacy, single) is unused and can be removed from env.
- Webhook payloads and outbound calls are both pinned `2026-03-25.dahlia`; future
  version bumps are a deliberate two-step: bump `STRIPE_API_VERSION` (outbound), then
  re-provision endpoints (inbound), with compat audit in between.
- Event log self-prunes after `STRIPE_WEBHOOK_EVENT_RETENTION_DAYS` (default 90).
