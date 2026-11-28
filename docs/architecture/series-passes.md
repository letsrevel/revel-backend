# Series Passes (Season Tickets)

A **series pass** is a season ticket sold on an `EventSeries`: the buyer pays once and receives a real `Ticket` for **every covered event** in the series. Pricing is **pro-rata** — the price drops as covered events pass — and a pass is purchasable only while at least two covered events remain. Pass purchases consume the **same tier capacity** as direct sales: each pass maps to one existing ticket tier per covered event, and pass holders draw from that tier's pool.

This page covers the data model, the pro-rata quote, the purchase flow and its locking rules, the penny-exact payment split, check-in, coverage extension, cancellation/refunds, and lifecycle integrity for stranded checkouts.

!!! note "Materialized, not virtual"
    Like [recurring-series occurrences](recurring-series.md), pass coverage is materialized: purchase creates one per-event `Ticket` row per covered future event (`held_pass` FK set). Downstream systems — attendee lists, capacity, statistics, check-in — see ordinary tickets; only notifications and self-cancellation treat pass tickets specially.

---

## Data model

All in `src/events/models/series_pass.py` (additive migration `0091`; `0118` adds the purchase `attribution` on `HeldSeriesPass`).

### `SeriesPass` — the product

| Field | Type | Notes |
|---|---|---|
| `event_series` | FK → `EventSeries`, CASCADE | The covered series. |
| `name` / `description` | str / Markdown | Unique per series (`unique_series_pass_name`). |
| `price` / `pro_rata_discount` / `currency` | Decimal / Decimal / str | See [pricing](#pricing-the-pro-rata-quote). |
| `payment_method` | `TicketTier.PaymentMethod` | `ONLINE` / `OFFLINE` / `FREE`. `AT_THE_DOOR` rejected in `clean()`. |
| `purchasable_by` | `TicketTier.PurchasableBy` | `PUBLIC` / `MEMBERS` / `NON_MEMBERS`. Invitation-restricted values rejected in `clean()`, as is `NON_MEMBERS` combined with `MEMBERS_ONLY` visibility (a pass members cannot buy must not be visible to members only). |
| `visibility` | `VisibilityMixin.Visibility` | Same five-level scheme as events/tiers; filtered by `pass_visible_to_user`. |
| `sales_start_at` / `sales_end_at` | DateTime, nullable | Sales window. |
| `is_active` | bool | Master switch. |
| `total_quantity` / `quantity_sold` | int (nullable) / int | Optional holder cap; counter maintained under locks. |

`clean()` also forbids changing `currency` once tier links exist and changing `payment_method` while non-cancelled holders exist. Changing `purchasable_by` is refused while a linked tier has a stricter rule than the new one (see below).

### `SeriesPassTierLink` — coverage

One row per covered event: `(series_pass, event, tier)` with `unique_series_pass_event`. `clean()` enforces: tier belongs to the event, event belongs to the pass's series, tier currency matches the pass, the tier uses neither assigned seating nor PWYC, and (for new or changed links only, so older links stay re-saveable) the tier's purchase rule is not **stricter than the pass's** (`tier_rule_stricter_than_pass` in `src/events/utils/purchase_rules.py`: a tier restricted to membership tiers, or whose `purchasable_by` admits fewer buyers than the pass's, cannot be covered, since the pass would hand out tickets the buyer could not buy directly). The same rule holds on the tier side after the link (#1341): `TicketTier.clean()` refuses a change of `purchasable_by` that makes a covered tier stricter than a pass that covers it, and `update_ticket_tier` refuses a first membership-tier restriction on a covered tier. As on the pass side, the check runs only when the rule changes, so an older mismatched link does not block other edits. The Django admin writes the membership-tier restriction after the save, so the admin does not check that one. The event side holds too (#1598): `Event.clean_fields()` (through `assert_event_change_keeps_coverage` in `src/events/models/series_pass.py`) refuses an `event_type` change away from PUBLIC, and any `event_series` change, while the event has a series pass link of any pass, active or not. The API answers 400 on that field, and the Django admin shows the error on the field. The check compares with the stored row, so an event that broke the rule earlier stays saveable for other edits. It runs in `clean_fields()`, not `clean()`, because only that step gets `exclude`: a save that does not write either field (for example `save(update_fields=["attendee_count"])`) makes no extra query, and the links are read only when one of the two fields changes. `requires_ticket` needs no check: it is create-only in the API and in the admin. A new link through `add_tier_links` (the API, or an event update with new links) and a change of the event, the tier or the pass lock the same row, so they cannot both pass at the same time (#1618; the lock order is in `docs/engineering-notes.md`). The Django admin link forms take no such lock and do not run the coverage gate.

Coverage is gated at enable time by `validate_events_coverable`: **non-recurring series only**, and each event OPEN, `requires_ticket` and `PUBLIC` (neither invitation-only nor members-only). ADMISSION questionnaires no longer block coverage (#1320); purchase requires them instead, see [Admission questionnaires](#admission-questionnaires).

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

quantized to cents, where `passed_events` counts tier-linked events with `start < now` **or** status `CANCELLED` (each event once). The quote is **not purchasable** when the pass is inactive, outside its sales window, sold out (`quantity_sold >= total_quantity`), when fewer than **2** covered events remain — at that point buyers should just buy a normal ticket — or when any upcoming covered event is not `OPEN` (the `EventStatusGate` condition; the reason is `Event is not open.`). Per-user gates (blacklist, profile) are never part of the quote.

**Cancelled covered events (#1494).** A `CANCELLED` covered event is treated like an event that has started: it gives one discount step (also when it is in the past, but never two), it does not count as remaining, and it does not block the sale. The purchase, the questionnaire gate, the activation backfill and the coverage extension issue no ticket for it, and the pass PDF, the wallet pass's representative event and the holder's `remaining_event_count` leave it out. A `DRAFT` (or any other non-`OPEN`) upcoming covered event still blocks the sale. When fewer than two non-cancelled upcoming events remain, the pass is not purchasable (`Not enough remaining events`). When the organizer un-cancels the event (`CANCELLED` to any other status, `OPEN`, `CLOSED` or `DRAFT`, in `event_update_service.update_status`, before the event starts), `events.materialize_series_pass_holders` is dispatched on commit for each pass that covers it: ACTIVE holders without a ticket for the event (for example, those who bought while it was cancelled) get one free of charge, with the usual capacity and blacklist checks and a `SERIES_PASS_EXTENDED` notification. The discount they got stays. The organizer still cannot remove a covered event from a pass that has holders.

**Status read under the event lock (#1496).** `update_status` locks the event row first, so every pass writer reads the covered events again under its own event locks (`series_pass_event_checks.lock_events_and_find_full`, which returns each event's status and start, the full ids and the `CANCELLED` ids). The extension task and the activation backfill skip, and log, an event that was cancelled or has started since their lock-free read. The purchase locks every covered event that has not started, the cancelled ones too, and computes the quote again under the locks, from the locked pass row and with a new `now`. Under the locks it answers 409 (`The events of this pass changed. Please try again.`) when the locked quote's remaining events are not exactly the events that get a ticket (a cancel or un-cancel, a deleted event, a moved start, an event that started meanwhile, or a new link), when a non-cancelled covered event is no longer `OPEN` (owners and staff are exempt, as from the status gate), or when the price, the payment method, the buyer rule or the currency of the pass changed. Right after the pass lock, before any other locked check, it also answers this 409 when the pass is now in another series, or its series in another organization: the checks before the lock used the series and the organization of the first read (#1577). After a lock wait, Postgres reads again only the locked pass row, and a joined row keeps the snapshot of the locking statement. So the organization of the series is read in a new statement after the lock. The series row is not locked, so a move of the series that commits after this read is not seen, the same limit as the visibility check. These checks run before the "sold out" 429, so a changed event answers 409. The checkout route also asks the service (`check_visibility=True`) to check the route's visibility rule again: an active pass with `pass_visible_to_user` on the locked pass row, and the series the buyer can see, read after the lock wait. A pass or series hidden after the route's lock-free check answers 404, as the route does. A change that keeps the pass visible is not refused. A change of the pass waits for the pass lock, so the check is exact for the pass. The series and organization rows are not locked, so it narrows the window for them. The lock-free gates before the locks (eligibility, questionnaires) still run on the route's read, so a pass hidden in between can get their 400 or 409 first (#1566). When the locked quote is no longer purchasable (for example, sales stopped), the 409 gives the quote's reason. Because a cancelled event is locked as well, an un-cancel that races a purchase waits for it, and its holder materialization then sees the new pass.

**Reconcile sweep (#1536).** Both holder materialization dispatches (coverage extension and un-cancel) are robust `on_commit` callbacks: a failed enqueue is logged and the request succeeds. The beat task `events.reconcile_series_pass_tickets` (every 15 minutes) finds each ACTIVE held pass with no ticket row of any status for a covered event that is not cancelled and has not started, and runs the extension for that holder, with the same capacity and blacklist checks. A ticket row of any status counts, here and in the extension itself (read under the pass lock), so a ticket that the organizer cancelled is never issued again by the extension, the un-cancel or the sweep. A holder that was skipped (full tier or event, blacklisted) is tried again on each run, so a freed seat gives the holder the ticket.

The purchase itself skips the quote's status check and runs the real per-event gate instead, which carries the eligibility payload and exempts owners and staff. The quoted price is snapshotted onto `HeldSeriesPass.price_paid` at purchase; later coverage changes never reprice an issued pass.

---

## Purchase flow

`SeriesPassPurchaseService` (`src/events/service/series_pass_purchase.py`) is the request-scoped workflow class (mirroring `BatchTicketService`). `purchase()` returns either a `HeldSeriesPass` (free/offline) or a Stripe checkout URL.

1. **Light checks** (no locks): blacklist, `purchasable_by` check (`MEMBERS` needs an eligible member, `NON_MEMBERS` refuses one, both 403; owners and staff exempt), the [admission questionnaire gate](#admission-questionnaires), quote purchasability, duplicate-pass check, and the **per-event gates** a normal ticket purchase applies to every covered *future* event: `BlacklistGate` (hard and fuzzy name match), `EventStatusGate` (event must be OPEN) and `FullProfileGate` (when the event requires a full profile). They reuse the eligibility gate classes through `EligibilityService.check_gates` (`series_pass_event_checks.py`); owners and staff are exempt, as in the normal flow. These checks run before the pass row lock, so a refused buyer never holds it. A failing event refuses the whole purchase with a 400 carrying the eligibility payload. `MembershipGate`, `InvitationGate`, `TicketSalesGate` and the availability gate are not run: the pass-level `purchasable_by` rule, the pass quote and the tier/event capacity assertions cover them.
2. **Lock the pass row** (`select_for_update`), re-check `total_quantity` under the lock.
3. **Lock all mapped tiers pk-ordered**, check capacity on every covered *future* event — **all-or-nothing**: one sold-out tier aborts with a 429.
   **Then lock the covered events pk-ordered** and check event-level capacity (`max_attendees` / venue capacity, counted with the same rules as batch checkout: non-cancelled tickets plus pending unexpired non-cutoff waitlist offers, the buyer's own pending offer counts like anyone else's, since the pass flows never claim it): a full event aborts the whole purchase with a 429 `Event {name} is sold out.`, nothing created or incremented. Sector caps are not checked (pass tickets carry no sector).
4. **Create the `HeldSeriesPass`** — a duplicate-purchase race loses on the conditional unique constraint and is mapped to `SeriesPassNotPurchasableError` → **409**.
5. **Materialize tickets** via `bulk_create` (future events only; ACTIVE for free passes, PENDING otherwise). `bulk_create` deliberately skips `post_save` signals so no per-ticket emails fire.
6. **Increment** each tier's `quantity_sold` and the pass's counter.
7. Branch by payment method: free → activate + notify; offline → return the PENDING pass (staff confirms later); online → return `(held_pass, reservation_id)`. The controller answers `requires_payment=true` with the `reservation_id`, and the client calls `POST /series-passes/reservations/{reservation_id}/checkout-session` to create the single Stripe session outside the contended locks (idempotent; 404 for an unknown, expired or foreign reservation).

Tickets issued here (and on offline confirmation) go through `day_membership_service.on_tickets_issued`, because `bulk_create` skips the signal that grants a [day membership](member-register.md#day-memberships) for tiers with `grants_membership_tier`. For the same reason they go through `series_pass_service.remove_holder_from_waitlists` (#1311): after commit, the holder leaves the waitlist of each event that the pass gave a ticket for. The timing is that of the per-ticket receiver: an ACTIVE ticket always (free passes, extensions, backfills, the offline confirmation), a PENDING ticket only for an offline pass. An online pass ticket becomes ACTIVE with a per-ticket save in the webhook, so the receiver removes the holder then.


!!! danger "Lock ordering invariant: pass row, then tiers pk-ordered, then events pk-ordered"
    Batch checkout locks tier before event, so the pass flows lock covered `Event` rows last. Every writer that touches pass + tier counters — purchase, whole-pass cancel, offline confirm, backfill, the extension task, all four expiry routes — takes the pass/held-pass row lock **before** the pk-ordered tier locks, and (purchase, backfill, extension) takes event locks after the tiers. Keep this order in any new code path or you introduce a deadlock.

### Offline confirmation

`confirm_held_pass_payment` (admin endpoint) flips pass + tickets PENDING→ACTIVE under a re-lock with a status re-check (double-confirm safe). Per-ticket `price_paid` is set by splitting `held_pass.price_paid` across tickets with `distribute_amount_across_items` in deterministic `(event.start, pk)` order.

### Admission questionnaires

A pass must not bypass the event [eligibility pipeline](eligibility-pipeline.md), so purchase is gated by `_assert_series_questionnaires` → `check_series_questionnaires` (`src/events/service/series_pass_questionnaires.py`, #1321):

- **Which**: published ADMISSION questionnaires linked to the series, plus those linked to each upcoming covered event. One linked to both is required once, as a series one. Questionnaires with `members_exempt` are skipped for eligible members; owners and staff bypass the gate.
- **How often**: a series-level questionnaire, or an event-only one that is not `per_event`, is answered once. A `per_event` questionnaire needs a submission scoped to each covered event carrying it (for a series-level `per_event` one, a submission made for any event of the series covers the pass).
- **Invitations never waive** these questionnaires.
- **Verdict**: one `EventUserEligibility` listing every missing questionnaire id and pointing at the first group's event, with `questionnaire_events` naming, per missing questionnaire, the event whose `/events/{event_id}/questionnaire/{questionnaire_id}` endpoint accepts the submission. If the event it points at can no longer take submissions, the verdict is `application_deadline_passed` with no `next_step`.
- Holders are **not** gated on a questionnaire attached to an event linked after purchase: extension tickets are issued anyway (see the `ponytail` note in `series_pass_service`).

---

## Payment split & VAT (online passes)

`create_series_pass_checkout_session` (`src/events/service/stripe_service.py`):

- **One Stripe session**, a single line item at the locked-in quote price, `held_pass_id` in metadata, connected account + `application_fee_amount` as with regular checkouts.
- **N `Payment` rows** — one per materialized ticket, all sharing the `stripe_session_id`. The total, the platform-fee gross, and the platform-fee VAT are each split **penny-exact** with `distribute_amount_across_items` (`vat_service`; remainder cents go to the first items, sums always reconcile).
- **Per-tier VAT**: each Payment's `net_amount` / `vat_amount` / `vat_rate` come from *its own ticket's tier* (`get_effective_vat_rate(tier.vat_rate, org.vat_rate)`), so a pass spanning tiers with different VAT rates stays tax-correct. See [Billing & VAT](billing-and-vat.md).
- Buyer billing info is snapshotted for invoicing only. Passes always charge org-rate VAT on the gross price — the correct treatment under Art. 53 (season tickets are admission, Art. 32 IR 282/2011), so there is no per-buyer VAT re-resolution to add (#868).

Activation happens in the `checkout.session.completed` webhook: an idempotent conditional `.update()` flips PENDING→ACTIVE, then `backfill_missing_tickets` grants tickets for any events linked while the pass sat PENDING, then the single pass-level purchase notification fires on commit. The backfill counts a ticket of the pass of any status, as the extension and the reconcile sweep do, so it never issues a cancelled pass ticket again (#1565). An abandoned checkout does not reach the backfill: its pass is cancelled, or its PENDING tickets are deleted.

---

## Check-in

`POST /event-admin/{event_id}/tickets/{code}/check-in` takes a **string code** (this renamed the former `ticket_id` UUID param — a breaking change shipped in 1.68.0):

- a plain ticket UUID checks in that ticket, unchanged;
- `series:<uuid>` is resolved by `resolve_check_in_ticket_id` to the holder's non-cancelled ticket **for the scanned event**.

Malformed codes are rejected by a path-regex (`CHECK_IN_CODE_PATTERN`) before touching the ORM, and the response shape is identical for both code forms — scanners don't need to know which QR they read. For PENDING pass tickets, the *pass's* payment method (not the tier's) decides whether check-in is allowed.

---

## Extending coverage

Events added to a series after passes were sold are **not** covered automatically — the organizer extends explicitly, either via `POST .../passes/{pass_id}/tier-links` or inline `series_pass_links` on event create/update. `add_tier_links` is idempotent per (event, tier) and dispatches the Celery task **`events.materialize_series_pass_holders`** via `transaction.on_commit`.

The task processes each ACTIVE holder in its own transaction (pass row locked, ACTIVE re-checked — safe against a concurrent cancel), locks tiers pk-ordered, and grants the new tickets **free of charge** (`price_paid=0.00`). Full tiers and events at event-level capacity are skipped and reported, not failed, and a holder who is now blacklisted for the organization (hard, or fuzzy name match without membership or whitelist) is skipped entirely (owners and staff are exempt from that check, as in the purchase); the same applies to the activation-time backfill (skips are logged). The blacklist check runs before the tier locks. Events the holder already has a ticket for are dropped first, so they are never reported as skipped. Each holder gets one `SERIES_PASS_EXTENDED` notification.

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
| `SERIES_PASS_PURCHASED` | Holder only. Org staff/owners get `STAFF_SERIES_PASS_PURCHASED` (gated on `manage_tickets`). |
| `SERIES_PASS_EXTENDED` | Holder only. |
| `SERIES_PASS_CANCELLED` | Holder only; carries refunded amount and cancelled-ticket count. Org staff/owners get `STAFF_SERIES_PASS_CANCELLED` (gated on `manage_tickets`). |

Per-ticket purchase/confirm/cancel signals early-return for any ticket with `held_pass_id` (belt and suspenders on top of the `bulk_create` signal skip). Templates live in `notifications/service/templates/series_pass_templates.py` with email + in-app bodies translated de/fr/it.

---

## Files: PDF & Apple Wallet

`series_pass_file_service.py` mirrors the ticket file service: get-or-generate with a SHA-256 content hash over the held pass, the pass product, **and every covered event's `updated_at` and `status`** — so extending coverage, editing a covered event or cancelling it invalidates the cache (a cancel saves with `update_fields`, so `updated_at` alone would not change). The PDF (`create_series_pass_pdf`) and the `.pkpass` (`wallet/apple/generator.py:generate_series_pass`) both embed the `series:<uuid>` QR; the wallet pass shows the soonest upcoming non-cancelled covered event and expires at the latest non-cancelled covered event's end (all covered events when every one is cancelled). Cached files are swept by `events.cleanup_ticket_file_cache` once the last covered event has ended.

---

## API

**Public** — `SeriesPassController` (`/series-passes`, `OptionalAuth`, visibility-filtered):

The pass routes check the series with `EventSeries.objects.for_user`, the organization rule. A series page can also be visible through a public event of the series (`viewable_for_user`, #1599), but its passes then stay hidden: a visitor who sees a private organization's series only through a public event sees no pass and cannot buy one.


| Method | Path | Purpose |
|---|---|---|
| `GET` | `/event-series/{series_id}` | List a series' visible passes. |
| `GET` | `/{pass_id}/quote` | Pro-rata quote + purchasability. For an authenticated buyer it also carries `questionnaires_missing`, `questionnaire_event_id` and `questionnaire_events` (one `{questionnaire_id, event_id}` per missing questionnaire; a `per_event` one appears once per covered event). |
| `POST` | `/{pass_id}/checkout` | Purchase (JWT). Free/offline: returns `held_pass`. Online: `requires_payment=true` plus `reservation_id`. 400 carries the eligibility payload (incl. `questionnaire_events`) when an admission questionnaire is missing, pending or failed; 403/404/409/422/429. |
| `POST` | `/reservations/{reservation_id}/checkout-session` | Second step of online checkout: creates (or reuses) the Stripe session and returns `checkout_url`. |
| `GET` | `/me` | The caller's passes (paginated). |
| `GET` | `/me/{held_pass_id}/pdf` · `/pkpass` | Downloads (302 to signed URL; 503 if Wallet unconfigured). |
| `GET` | `/me/{held_pass_id}/wallet/google` | Redirect to the Google Wallet save link (`?format=json` returns it as JSON; 503 if unconfigured). |

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
- [Eligibility Pipeline](eligibility-pipeline.md) — the event gates and tier-level purchase rules a pass purchase mirrors, including the admission questionnaires it requires.
- [Member Register & Day Memberships](member-register.md) — day memberships granted by pass tickets.
- [Notifications](notifications.md) — multi-channel dispatch for the three pass-level types.
