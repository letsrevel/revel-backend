# Series Passes (Season Tickets)

A **series pass** is a season ticket sold on an `EventSeries`: the buyer pays once and receives a real `Ticket` for **every covered event** in the series. Pricing is **pro-rata** — the price drops as covered events pass — and a pass is purchasable only while at least two covered events remain. Pass purchases consume the **same tier capacity** as direct sales: each pass maps to one existing ticket tier per covered event, and pass holders draw from that tier's pool.

This page covers the data model, the pro-rata quote, the purchase flow and its locking rules, the penny-exact payment split, check-in, coverage extension, cancellation/refunds, and lifecycle integrity for stranded checkouts.

!!! note "Materialized, not virtual"
    Like [recurring-series occurrences](recurring-series.md), pass coverage is materialized: purchase creates one per-event `Ticket` row per covered future event (`held_pass` FK set). Downstream systems — attendee lists, capacity, statistics, check-in — see ordinary tickets; only notifications and self-cancellation treat pass tickets specially.

---

## Data model

All in `src/events/models/series_pass.py` (single additive migration `0091`).

### `SeriesPass` — the product

| Field | Type | Notes |
|---|---|---|
| `event_series` | FK → `EventSeries`, CASCADE | The covered series. |
| `name` / `description` | str / Markdown | Unique per series (`unique_series_pass_name`). |
| `price` / `pro_rata_discount` / `currency` | Decimal / Decimal / str | See [pricing](#pricing-the-pro-rata-quote). |
| `payment_method` | `TicketTier.PaymentMethod` | `ONLINE` / `OFFLINE` / `FREE`. `AT_THE_DOOR` rejected in `clean()`. |
| `purchasable_by` | `TicketTier.PurchasableBy` | Invitation-restricted values rejected in `clean()`. |
| `visibility` | `VisibilityMixin.Visibility` | Same five-level scheme as events/tiers; filtered by `pass_visible_to_user`. |
| `sales_start_at` / `sales_end_at` | DateTime, nullable | Sales window. |
| `is_active` | bool | Master switch. |
| `total_quantity` / `quantity_sold` | int (nullable) / int | Optional holder cap; counter maintained under locks. |

`clean()` also forbids changing `currency` once tier links exist and changing `payment_method` while non-cancelled holders exist.

### `SeriesPassTierLink` — coverage

One row per covered event: `(series_pass, event, tier)` with `unique_series_pass_event`. `clean()` enforces: tier belongs to the event, event belongs to the pass's series, tier currency matches the pass, and the tier uses neither assigned seating nor PWYC.

Coverage is gated at enable time by `validate_events_coverable`: **non-recurring series only**, each event OPEN + `requires_ticket` + not PRIVATE, and no ADMISSION questionnaire targeting the series or any covered event (a pass must not bypass the [eligibility pipeline](eligibility-pipeline.md)).

### `HeldSeriesPass` — the buyer's pass

| Field | Type | Notes |
|---|---|---|
| `series_pass` | FK, **PROTECT** | Deleting a pass with holders 409s (`SeriesPassHasHoldersError`). |
| `user` | FK → `RevelUser`, CASCADE | One non-cancelled pass per user per product (`unique_active_held_pass_per_user`, conditional). |
| `status` | `HeldSeriesPassStatus` | `PENDING` / `ACTIVE` / `CANCELLED`. |
| `price_paid` | Decimal | The quote price locked in at purchase. |
| `stripe_session_id` | str, nullable | For online passes; drives webhook activation and expiry sweeps. |
| `pdf_file` / `pkpass_file` / `file_content_hash` | ProtectedFileField / str | Cached deliverables, see [Files](#files-pdf-apple-wallet). |

`HeldSeriesPass.qr_payload` returns `series:<uuid>` (`QR_PREFIX = "series:"`) — the single source of truth used by the PDF, the Apple Wallet pass, and check-in resolution.

### `Ticket.held_pass`

Nullable FK on `Ticket` (`on_delete=RESTRICT`), `related_name="tickets"`, with a conditional unique constraint (one non-cancelled ticket per pass per event). A ticket with `held_pass_id` set is a *pass ticket*: it is excluded from per-ticket notifications and from self-service cancellation (`CancellationBlockReason.PART_OF_SERIES_PASS`).

---

## Pricing: the pro-rata quote

`get_quote(series_pass)` in `src/events/service/series_pass_service.py` returns a frozen `SeriesPassQuote`:

```
price = max(pass.price − passed_events × pass.pro_rata_discount, 0.00)
```

quantized to cents, where `passed_events` counts tier-linked events with `start < now`. The quote is **not purchasable** when the pass is inactive, outside its sales window, sold out (`quantity_sold >= total_quantity`), or when fewer than **2** covered events remain — at that point buyers should just buy a normal ticket. The quoted price is snapshotted onto `HeldSeriesPass.price_paid` at purchase; later coverage changes never reprice an issued pass.

---

## Purchase flow

`SeriesPassPurchaseService` (`src/events/service/series_pass_purchase.py`) is the request-scoped workflow class (mirroring `BatchTicketService`). `purchase()` returns either a `HeldSeriesPass` (free/offline) or a Stripe checkout URL.

1. **Light checks** (no locks): blacklist, `purchasable_by` membership check, quote purchasability, duplicate-pass check.
2. **Lock the pass row** (`select_for_update`), re-check `total_quantity` under the lock.
3. **Lock all mapped tiers pk-ordered**, check capacity on every covered *future* event — **all-or-nothing**: one sold-out tier aborts with a 429.
4. **Create the `HeldSeriesPass`** — a duplicate-purchase race loses on the conditional unique constraint and is mapped to `SeriesPassNotPurchasableError` → **409**.
5. **Materialize tickets** via `bulk_create` (future events only; ACTIVE for free passes, PENDING otherwise). `bulk_create` deliberately skips `post_save` signals so no per-ticket emails fire.
6. **Increment** each tier's `quantity_sold` and the pass's counter.
7. Branch by payment method: free → activate + notify; offline → return the PENDING pass (staff confirms later); online → one Stripe checkout session.

!!! danger "Lock ordering invariant: pass row first, then tiers pk-ordered"
    Every writer that touches pass + tier counters — purchase, whole-pass cancel, offline confirm, backfill, the extension task, all four expiry routes — takes the pass/held-pass row lock **before** the pk-ordered tier locks. Keep this order in any new code path or you introduce a deadlock.

### Offline confirmation

`confirm_held_pass_payment` (admin endpoint) flips pass + tickets PENDING→ACTIVE under a re-lock with a status re-check (double-confirm safe). Per-ticket `price_paid` is set by splitting `held_pass.price_paid` across tickets with `distribute_amount_across_items` in deterministic `(event.start, pk)` order.

---

## Payment split & VAT (online passes)

`create_series_pass_checkout_session` (`src/events/service/stripe_service.py`):

- **One Stripe session**, a single line item at the locked-in quote price, `held_pass_id` in metadata, connected account + `application_fee_amount` as with regular checkouts.
- **N `Payment` rows** — one per materialized ticket, all sharing the `stripe_session_id`. The total, the platform-fee gross, and the platform-fee VAT are each split **penny-exact** with `distribute_amount_across_items` (`vat_service`; remainder cents go to the first items, sums always reconcile).
- **Per-tier VAT**: each Payment's `net_amount` / `vat_amount` / `vat_rate` come from *its own ticket's tier* (`get_effective_vat_rate(tier.vat_rate, org.vat_rate)`), so a pass spanning tiers with different VAT rates stays tax-correct. See [Billing & VAT](billing-and-vat.md).
- Buyer billing info is snapshotted only in v1 — no reverse-charge re-resolution for pass checkouts yet.

Activation happens in the `checkout.session.completed` webhook: an idempotent conditional `.update()` flips PENDING→ACTIVE, then `backfill_missing_tickets` grants tickets for any events linked while the pass sat PENDING, then the single pass-level purchase notification fires on commit.

---

## Check-in

`POST /event-admin/{event_id}/tickets/{code}/check-in` takes a **string code** (this renamed the former `ticket_id` UUID param — a breaking change shipped in 1.68.0):

- a plain ticket UUID checks in that ticket, unchanged;
- `series:<uuid>` is resolved by `resolve_check_in_ticket_id` to the holder's non-cancelled ticket **for the scanned event**.

Malformed codes are rejected by a path-regex (`CHECK_IN_CODE_PATTERN`) before touching the ORM, and the response shape is identical for both code forms — scanners don't need to know which QR they read. For PENDING pass tickets, the *pass's* payment method (not the tier's) decides whether check-in is allowed.

---

## Extending coverage

Events added to a series after passes were sold are **not** covered automatically — the organizer extends explicitly, either via `POST .../passes/{pass_id}/tier-links` or inline `series_pass_links` on event create/update. `add_tier_links` is idempotent per (event, tier) and dispatches the Celery task **`events.materialize_series_pass_holders`** via `transaction.on_commit`.

The task processes each ACTIVE holder in its own transaction (pass row locked, ACTIVE re-checked — safe against a concurrent cancel), locks tiers pk-ordered, and grants the new tickets **free of charge** (`price_paid=0.00`). Full tiers are skipped and reported, not failed. Each holder gets one `SERIES_PASS_EXTENDED` notification.

---

## Cancellation & refunds

| Path | Behavior |
|---|---|
| **Holder self-cancel of a pass ticket** | Blocked — `cancellation_service` short-circuits with `PART_OF_SERIES_PASS` (409) before any other check. Per-ticket confirm/refund flows likewise reject pass tickets. |
| **Organizer cancels a covered event** | The standard event-cancellation refund path issues each holder a partial refund of *their per-event `Payment` share* (per-tier VAT-accurate). |
| **Organizer cancels a whole pass** | `cancel_held_pass`: re-lock + status re-check, cancel future non-checked-in tickets, refund SUCCEEDED payments (idempotency key `refund:{ticket.id}`), fail pending ones, decrement tier + pass counters, expire a still-live Stripe session, one `SERIES_PASS_CANCELLED` notification. |
| **Deleting a pass / removing a tier link** | 409 (`SeriesPassHasHoldersError`) while non-cancelled holders exist. Series deletion surfaces the `HeldSeriesPass` PROTECT the same way. |

---

## Lifecycle integrity: stranded checkouts

An online buyer who abandons Stripe leaves a PENDING pass holding capacity. All four expiry routes — the `events.cleanup_expired_payments` beat sweep, resumed-checkout cleanup, explicit checkout cancel, and the `payment_intent.canceled` webhook — call `expire_stranded_held_passes` **before** releasing tier capacity (pass-before-tiers order). The flip to CANCELLED is an atomic conditional-UPDATE claim, so concurrent sweeps can't double-decrement `quantity_sold`; tier counters are released by each route's own logic, floored at zero.

---

## Notifications

Three pass-level types (in `notifications/enums.py`), replacing per-ticket noise:

| Type | Audience |
|---|---|
| `SERIES_PASS_PURCHASED` | Holder + org staff/owners (staff gated on `manage_tickets`). |
| `SERIES_PASS_EXTENDED` | Holder only. |
| `SERIES_PASS_CANCELLED` | Holder + staff/owners; carries refunded amount and cancelled-ticket count. |

Per-ticket purchase/confirm/cancel signals early-return for any ticket with `held_pass_id` (belt and suspenders on top of the `bulk_create` signal skip). Templates live in `notifications/service/templates/series_pass_templates.py` with email + in-app bodies translated de/fr/it.

---

## Files: PDF & Apple Wallet

`series_pass_file_service.py` mirrors the ticket file service: get-or-generate with a SHA-256 content hash over the held pass, the pass product, **and every covered event's `updated_at`** — so extending coverage or editing a covered event invalidates the cache. The PDF (`create_series_pass_pdf`) and the `.pkpass` (`wallet/apple/generator.py:generate_series_pass`) both embed the `series:<uuid>` QR; the wallet pass shows the soonest upcoming covered event and expires at the latest covered event's end. Cached files are swept by `events.cleanup_ticket_file_cache` once the last covered event has ended.

---

## API

**Public** — `SeriesPassController` (`/series-passes`, `OptionalAuth`, visibility-filtered):

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/event-series/{series_id}` | List a series' visible passes. |
| `GET` | `/{pass_id}/quote` | Pro-rata quote + purchasability. |
| `POST` | `/{pass_id}/checkout` | Purchase (JWT). Returns `checkout_url` **xor** `held_pass`; 403/404/409/429. |
| `GET` | `/me` | The caller's passes (paginated). |
| `GET` | `/me/{held_pass_id}/pdf` · `/pkpass` | Downloads (302 to signed URL; 503 if Wallet unconfigured). |

**Admin** — `SeriesPassAdminController` (`/event-series-admin/{series_id}/passes`, requires `edit_event_series`):

| Method | Path | Purpose |
|---|---|---|
| `GET` / `POST` | `/` | List (with `holder_count`) / create. |
| `PATCH` / `DELETE` | `/{pass_id}` | Update / delete (409 with holders). |
| `POST` | `/{pass_id}/tier-links` | Extend coverage (triggers materialization). |
| `DELETE` | `/{pass_id}/tier-links/{event_id}` | Remove coverage (409 with holders). |
| `GET` | `/{pass_id}/holders` | Holder list (paginated, searchable). |
| `POST` | `/held/{held_pass_id}/confirm-payment` | Offline payment confirmation. |
| `POST` | `/held/{held_pass_id}/cancel` | Whole-pass cancel (optional `reason`). |

Attendee/ticket admin lists accept `?source=pass|direct`, and ticket schemas expose a `series_pass` annotation (prefetched — no N+1). Errors map in `events/exception_handlers.py`: coverage → 400, not-purchasable / has-holders → 409.

---

## Related reading

- [Recurring Series](recurring-series.md) — the `EventSeries` container passes attach to (passes require a *non-recurring* series).
- [Billing & VAT](billing-and-vat.md) — `distribute_amount_across_items` penny-exact splitting and effective VAT rates.
- [Eligibility Pipeline](eligibility-pipeline.md) — why series with ADMISSION questionnaires can't sell passes.
- [Notifications](notifications.md) — multi-channel dispatch for the three pass-level types.
