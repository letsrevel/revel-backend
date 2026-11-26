# Engineering Notes (deep gotchas)

Production-only failure modes that **cannot be reproduced in tests** (no pooler, transactions
roll back). Read these before touching Celery dispatch, long-running sweeps, or anything that
iterates a large queryset. Referenced from `CLAUDE.md` → Operating Principles & Project Rules.

## Celery Dispatch & Transactions (`transaction.on_commit`)

`settings.base` sets `DATABASES["default"]["ATOMIC_REQUESTS"] = True`, so **every HTTP
request runs inside one DB transaction** that commits only when the view returns
successfully. `.delay()` pushes the task to the broker **immediately**, regardless of
that transaction. If a worker picks the task up before the request commits, any
`Model.objects.get(pk=…)` for a row created in the same request raises `DoesNotExist`
(intermittent, load-dependent, hard to reproduce).

- **Default rule**: when dispatching a Celery task from anything reachable by an HTTP
  request (a controller, a service called by a controller, or a `post_save`/`post_delete`
  signal handler firing during a request), defer the dispatch:
  ```python
  from django.db import transaction

  transaction.on_commit(lambda: my_task.delay(arg))
  ```
  If the transaction rolls back, the dispatch is correctly skipped.
- **Don't wrap task-to-task chains**: code that only runs *inside* a Celery worker (a
  `@shared_task` calling another task) is not under `ATOMIC_REQUESTS`; bare `.delay()` is
  fine there unless the task opens its own `@transaction.atomic` block first.
- **Loop variables**: never `transaction.on_commit(lambda: task.delay(x))` inside a `for x
  in …` loop — the callback runs after the loop ends and captures the *last* `x`. Build
  the values first and dispatch them in a single `on_commit` closure, or bind eagerly with
  `functools.partial(task.delay, x)`.
- **Dispatch-then-`raise` (anti-enumeration) paths are the exception**: if a handler
  intentionally dispatches a task and then raises (rolling the request back) — and the task
  targets a row that *already existed* before this request — use a bare `.delay()`. An
  `on_commit` callback registered inside an atomic block that then rolls back is discarded,
  so the task would never run. See `accounts/service/account.register_user` /
  `send_verification_email_for_user(..., defer=False)`.
- **Tests**: pytest-django's default `django_db` rolls the wrapping transaction back, so
  `on_commit` callbacks never fire (and with `CELERY_TASK_ALWAYS_EAGER` the eager task
  never runs). A test asserting the dispatch/side effect must either wrap the trigger in
  the `django_capture_on_commit_callbacks(execute=True)` fixture or be marked
  `@pytest.mark.django_db(transaction=True)` with a docstring explaining why.

## Celery Task Names (always pin `name=`)

A bare `@shared_task` registers under Celery's **default name**, derived from the task's
import path: `<module>.<func>` (e.g. a task defined in `events/tasks.py` becomes
`events.tasks.<func>`). That couples the registered name to the *file location*, which bites
in two production-only ways:

- **`django_celery_beat` schedules break silently.** `PeriodicTask` rows (seeded by
  migrations or created in the admin) reference tasks by **name string**. Move or rename the
  module and the default name changes, but the DB row still points at the old string — the
  beat scheduler emits it, no worker is registered under that name, and the job just stops
  firing. No error at deploy time; it surfaces as "why didn't the nightly job run?"
- **In-flight messages die across a rolling deploy.** A task enqueued under the old name
  before deploy is picked up by a new worker that only knows the new name → `NotRegistered`.

**Rule:** every task pins an explicit `name=` (`@shared_task(name="events.cleanup_expired_payments")`),
so the registered name is independent of where the function lives and modules stay free to move
or split. When relocating an existing task, **keep its current name verbatim** (pin it to the
old default if it was bare) so beat rows and queued messages keep resolving.

Two guards enforce this:

- **`make task-names`** (`scripts/check_task_names.py`, in `make check` **and** CI) — a static
  AST check that fails if any `@shared_task`/`@app.task` lacks `name=`. No imports, no DB.
- **`python manage.py check_orphaned_beat_tasks`** — a runtime check that fails if any
  `PeriodicTask` row references a name no task is registered under. Catches manually-created
  rows the static check can't see; suitable as a deploy/health gate.

This is exactly why `events/tasks.py` and `accounts/tasks.py` could be split into packages, and
the stray `events/tasks_stripe.py` etc. folded in, without breaking a single beat schedule.

## Server-Side Cursors & Connection Pooling (`.iterator()`)

Production runs **PgBouncer in transaction-pooling mode**, so `DATABASES["default"]`
sets `DISABLE_SERVER_SIDE_CURSORS = True` whenever `USE_PGBOUNCER` is on (Django's
documented requirement for that mode). Keep this in mind when iterating querysets:

- **Never combine `QuerySet.iterator()` with a per-row `transaction.atomic()` commit
  inside the loop.** `.iterator()` opens a PostgreSQL **server-side cursor**; transaction
  pooling recycles the backend connection at every `COMMIT`, orphaning that cursor, so the
  next `fetchmany()`/`close()` raises `psycopg.errors.InvalidCursorName`. This is
  load-/data-dependent (only fires when the loop body runs ≥1 commit) and **cannot be
  reproduced in tests** (plain Postgres, no pooler, and psycopg3's `WITH HOLD` cursor
  survives commits on a single backend). It bit `expire_subscriptions_past_grace` and
  `close_polls_due` — see #458 and `docs/postmortems/0002-server-side-cursor-pgbouncer.md`.
- **Correct pattern for "snapshot a candidate set, then process each under its own lock":**
  materialize the IDs with `list(...)`, then loop and re-fetch each row with
  `select_for_update()` in its own `transaction.atomic()`:
  ```python
  ids = list(Model.objects.filter(...).values_list("id", flat=True))
  for pk in ids:
      with transaction.atomic():
          obj = Model.objects.select_for_update().get(pk=pk)
          ...  # re-check preconditions inside the lock, then save
  ```
  IDs are tiny, so `.iterator()` buys no real memory savings here anyway.
- **`.iterator()` is still fine** for read-only streaming with no mid-loop DB commits
  (e.g. dispatching Celery tasks per row). Note that with `DISABLE_SERVER_SIDE_CURSORS`
  it degrades to a client-side fetch-all — for genuinely large streams, batch explicitly.

## Calendar Dates: `timezone.localdate()`, not `timezone.now().date()`

`settings.TIME_ZONE = "Europe/Vienna"` with `USE_TZ = True`, so **the DB stores UTC but
Django's date lookups render in `TIME_ZONE`**. A `__date` lookup (`start__date`,
`current_period_end__date`, and the `__date__gte`/`__date__gt`/`__date__range` variants)
compiles to `(<col> AT TIME ZONE 'Europe/Vienna')::date` — a *local* calendar date. Pairing
it with `timezone.now().date()`, which is the *UTC* calendar date, makes the two sides speak
different calendars; they disagree for the 2 hours (1 in winter) before UTC midnight.

- **Default to `timezone.localdate()`** whenever you need "today" as a calendar date. It is
  what nearly every caller means: the date a human in the deployment's timezone would name.
  `timezone.now().date()` is only correct if you genuinely want the UTC calendar day, and
  nothing in this codebase does.
- **The bug is invisible in CI and in prod for most of the day.** It only fires inside the
  pre-UTC-midnight window, so a green test run at 10:00 is no evidence the logic is right.
  `send_subscription_renewal_reminders` shipped with `now().date()` against
  `current_period_end__date` and was correct only because its beat entry fires at 05:00 UTC
  (mid-morning in Vienna); moving the schedule would have silently shifted the whole reminder
  cohort by a day. If you must verify a date-window fix, run the test with the clock inside
  the window (or temporarily flip `TIME_ZONE` between `UTC` and `Europe/Vienna` — identical
  code should pass under both).
- **Monthly period arithmetic has the same requirement.** `generate_monthly_invoices` and
  `calculate_referral_payouts` derive "previous month" from today and then build the period
  bounds with `timezone.make_aware(datetime.combine(day, time.min))` — i.e. **local**
  midnights. The month must therefore be picked off the *local* calendar too, or a
  day-1 run can bill a local month that has not finished elapsing.
- **A caller-supplied date is fine as-is.** `events/filters.py`'s `filter_date` does
  `Q(start__date=date.date())` on an `AwareDatetime` from the request: the date comes from the
  caller's own offset, which is the intended contract, not from server "now".

## Online Checkout: Reserve/Session Split (`ATOMIC_REQUESTS` vs. `select_for_update`)

Online checkout (`BatchTicketService.create_batch`, series-pass purchase) locks the
`TicketTier`/`SeriesPass` row with `select_for_update()` to serialize capacity checks
against concurrent buyers. Under `ATOMIC_REQUESTS = True` (see above), that lock is held
for the *entire* request — it releases only when the wrapping transaction commits, i.e.
when the view returns. A single-request flow that also calls `stripe.checkout.Session.create`
(~2.5s round-trip, network-dependent) would therefore hold the row lock for the full
Stripe latency, serializing every buyer of a tier behind one Stripe call at a time —
unacceptable for popular tiers during a release rush. There is no way to drop the lock
mid-request without dropping the whole transaction, so the fix is structural: split the
single "buy" endpoint into **two requests**. The first (`reserve`) takes the lock, checks
capacity, creates `PENDING` `Ticket`/`HeldSeriesPass` rows and `Payment` rows (with
`stripe_session_id=""`), and returns — releasing the lock on commit. The second
(`/checkout-session`) runs lock-free and only talks to Stripe, then stamps the
Payment rows with the returned session id.

**`reservation_id`** (`Payment.reservation_id`, nullable UUID) is the grouping key that ties
a reserve step to its later session step: all Payment rows created by one reserve call share
the same `reservation_id`, minted by the reserve step and echoed back in the response
(`BatchCheckoutResponse`/`GuestCheckoutResponseSchema`/`SeriesPassCheckoutResponseSchema`).
The session step (`create_batch_session`/`create_series_pass_session` in
`events/service/stripe_service.py`) looks rows up by `reservation_id`, calls Stripe with
`idempotency_key=str(reservation_id)` (so a client retry after a network blip can't create a
second Stripe session for the same reservation), and `UPDATE`s those rows in place — it never
creates new Payment rows. Interactive reclaim paths (`cancel_pending_checkout`,
`_cleanup_expired_batch` in `events/service/pending_checkout.py`) group sibling payments the
same way: by `reservation_id` when present, falling back to `stripe_session_id` only for
legacy rows that predate the split.

**Two abandonment windows, two guarantees:**
- **Window B — client dies between reserve and session-stamp.** Structurally closed: a
  `Payment` row (status `PENDING`, `stripe_session_id=""`) always exists *before* Stripe is
  ever called, so "a paid Stripe session with zero matching Payment rows" is unreachable —
  the webhook can always reconcile, and a retried `/checkout-session` call is idempotent
  (same `reservation_id` → same Stripe idempotency key → safe to re-stamp). See the
  `test_payment_rows_exist_before_stripe_and_retry_recovers_after_stamp_failure` test for the
  real call-site injection proving this. Two guards keep the claim true against *concurrent
  reclaim during the (lock-free) Stripe call itself*: before calling Stripe, the session step
  atomically extends the reservation hold (a conditional `UPDATE` on `expires_at`, so the
  expiry sweep can't reclaim rows mid-call), and the post-call stamp checks its rowcount — if
  it matched zero rows (e.g. the buyer cancelled from a second tab while `Session.create` was
  in flight), the just-created session is best-effort expired and a 404 is raised instead of
  releasing the URL. An unreleased session is unpayable, so the invariant holds even if the
  expire call fails. Symmetrically, `cancel_pending_checkout` best-effort expires the batch's
  Stripe session after commit when it reclaims already-sessioned rows, closing the
  cancel-then-pay-the-open-tab variant.
- **Window A — client never returns to call `/checkout-session` at all** (abandons after
  reserve). These are reclaimed on expiry by `cleanup_expired_payments` (the periodic beat
  task in `events/tasks/payments.py`), which deletes the expired `PENDING` Payment/Ticket rows
  and decrements `TicketTier.quantity_sold` back to baseline. For series passes, the reserve
  step also creates a `PENDING` `HeldSeriesPass` with no Stripe session yet
  (`stripe_session_id=""`), which the session-id-based expiry (`expire_stranded_held_passes`)
  can't target — those are reclaimed instead via `expire_held_passes_for_tickets`, which walks
  the *expiring tickets* back to their `held_pass` and cancels it (decrementing
  `SeriesPass.quantity_sold`), used in the same three pre-session reclaim routes (beat task,
  `cancel_pending_checkout`, `_cleanup_expired_batch`); the webhook's `payment_intent.canceled`
  handler keeps the session-id-based expiry since by then a session always exists.

See issue #632 for the full design (Option F) and the task-by-task implementation log.

**Membership subscriptions deliberately do NOT use the reserve/session split.** Every
online subscription mutation (subscribe, cancel, pause/resume, change-plan, revive) takes
`select_for_update` on the *subscription* row and — under `ATOMIC_REQUESTS` — holds it
across its Stripe round-trips: the inner `transaction.atomic()` exit releases only a
savepoint, never the lock (code comments in `subscription.lifecycle` /
`subscription.stripe.plan_change` state this explicitly). This is acceptable where the
tier-row lock was not because the contended row is **per-member**: the blast radius is one
member's own requests, not every buyer of a hot tier. It is also currently *load-bearing*:
the held lock is what serializes Stripe echo-webhooks against the local mutation (the
webhook's own `select_for_update` blocks until the request commits, then its dispatch gates
see the updated flags and suppress duplicate notifications). Don't "fix" the lock without
replacing that serialization. Two webhook-side network calls exist, and only one follows the
resolve-before-lock discipline above: `_invoice_payment_intent_id`'s `Invoice.retrieve`
fallback resolves *before* the row lock, but `_backfill_initial_invoice`'s `Invoice.retrieve`
(on `checkout.session.completed`, self-healing a dropped `invoice.paid`) runs **while holding
the member's row lock** — the same accepted single-member-blast-radius trade as the request-side
mutations, and it is guarded: it is skipped entirely when the invoice id is already recorded, and
a Stripe failure propagates so the whole delivery rolls back and Stripe redelivers. The plan-row lock taken by
`ensure_plan_sales_capacity` (sale caps) is scoped the same way as the tier lock — the
lock itself only guards a count+insert, **but under `ATOMIC_REQUESTS` it survives until
request commit**, so in `start_online_subscription` (capped plans only) it is in fact held
across the Checkout-Session Stripe call that follows `create_subscription`. That serializes
concurrent subscribers to a *capped* plan behind one Stripe round-trip at a time — accepted
for now (capped-plan checkout traffic is a fraction of a hot tier's on-sale rush; the
subscribe-path docstring carries the same NOTE). The reserve/session split is the upgrade
path if a capped plan ever sees rush-level traffic.

## Lock order

One global order, everywhere a transaction locks more than one of these:

**`RevelUser` (the account row of the buyer or of the deleted account, `FOR NO KEY UPDATE`) → `Payment` (pk order)
→ `HeldSeriesPass` → `SeriesPass` → `TicketTier` (pk order) → `Event` → `Ticket` (pk order)
→ `DiscountCode` (pk order) → `MembershipRecord` → `TicketNumberSequence` (org-id order)**

**Account row first (#1476).** The deletion task (`accounts.tasks.delete_user_account`) locks the account row
before it locks or reads any holding of the account. Every checkout that writes a `Ticket`, `Payment` or
`HeldSeriesPass` for a buyer takes the same lock with `account_erasure.lock_account_for_checkout(user)` before its
first row lock: `BatchTicketService.create_batch` (after the VIES call, before the tier lock),
`SeriesPassPurchaseService.purchase` (before the pass row lock), `box_office.sell` (before the tier lock) and
`guest.claim_invitation_link` (a guest checkout or RSVP with an invitation link: before the claim, which locks the
token and the guest's `EventInvitation`; the deletion cascade deletes that invitation while it holds the account row). The
helper reads `erased_at` and `deletion_requested_at` under the lock and refuses with 403 when one is set or the row
is gone. So the two never overlap: the checkout commits first and the deletion sees its rows (it releases a PENDING
checkout and keeps an issued ticket with its holder snapshot), or the deletion commits first and the checkout writes
nothing. The data-export task takes the same lock for the whole export.

- The mode is `FOR NO KEY UPDATE` on both sides, not `FOR UPDATE` plus `FOR KEY SHARE`. Django's foreign keys are
  deferred, so every transaction that inserts a row for the account takes `FOR KEY SHARE` on the account row at its
  commit, while it still holds its other locks. `FOR NO KEY UPDATE` does not conflict with that, so the deletion
  cannot deadlock against such a transaction. `FOR UPDATE` would.
- A new path that holds one of the locks above must not lock or update a `RevelUser` row afterwards. Take the
  account lock first.
- The lock is on one account: two checkouts of the same buyer wait for each other, and so does the buyer's own
  token refresh. Checkouts of different buyers do not.
- Not serialized: tickets that a kept series pass gets later (`series_pass_service.materialize_tickets` from a
  backfill or an extension). That path already holds pass and tier locks, so it cannot take the account lock, and
  an erased holder must still get these tickets. They take the holder snapshot from the pass. A materialization
  that overlaps the erasure itself can write a ticket without the snapshot; staff then see "Deleted user".
  The threaded tests are in `events/tests/test_account_erasure/test_serialization.py`.

**`Organization` and `EventSeries` (image files and recurrence).** They sit immediately before `Event`:
`Organization` → `EventSeries` → `Event`. In the paths below, these row locks are `FOR NO KEY UPDATE`, so the
deferred foreign-key checks of other transactions (`FOR KEY SHARE` at commit) are not blocked. Some other paths
(the custom-domain, contact and membership services) still lock `Organization` with a plain `FOR UPDATE`.

- Recurrence (`recurrence_service.generate_series_events`, `cancel_occurrence`) locks the series, then the template
  (`duplicate_event`) or an occurrence.
- A series delete (`EventSeries.delete`) locks the series, then its events, and only then updates the series' unique
  `template_event`/`recurrence_rule` columns (that `UPDATE` takes `FOR UPDATE`). An organization delete locks the
  organization, then its series, then its events.
- The cascade deletes (event, ticket tier, event series, and the admin organization delete) take their locks in
  the global order before the model `delete()`, with `pending_checkout.lock_for_cascade_delete` (#1538):
  `Payment` → every `TicketTier` in scope → `Organization` → `EventSeries` → `Event` (`FOR UPDATE`, because
  the rows are deleted) → `Payment` again → `Ticket` → `DiscountCode`. A series pass extension or backfill
  locks the tier and then the event, so it and a delete of that event serialize on the tier: the delete sees
  the new ticket, or the extension sees the event gone and skips it. The second `Payment` lock is the one
  exception, for rows that a reservation committed between the first lock and the tier lock (see
  [Deletes that cascade pending checkouts](#deletes-that-cascade-pending-checkouts-1490)).
- The image-path locks (`common/thumbnails`: upload, derived-path write, image delete, admin image change) take one
  row of one model and change only image columns. The malware quarantine (`files.detach_image_everywhere`) locks
  every row that names a flagged image, model by model in this order (`RevelUser`, `Organization`, `EventSeries`,
  `Event`, then the rest) and in pk order within a model. An admin change form keeps that lock until the form saves,
  including its inlines. The path-level advisory locks (`common.thumbnails.files.lock_image_paths`) come last: no
  row lock is taken after a path lock (the reference check after them only reads).

**Ticket-ownership lemma.** Every transaction that touches an existing ticket that has a `Payment`
locks that `Payment` first. So, once a transaction holds the `Payment`, no other path can cancel,
activate or release that ticket. A path may trust the ticket status only when it reads it in a
statement that starts **after** it holds the `Payment` lock, and never from the locking statement
itself (for example through `select_related("ticket")`).
Under READ COMMITTED, a statement that waited for a row lock is re-checked only if the locked row
changed. A cancel locks the `Payment` but updates only the `Ticket`, `TicketTier` and
`DiscountCode`, so the waiting statement would keep the old ticket state. Re-check `CHECKED_IN`
after the ticket lock.

Paths that follow the lemma: user cancel (`cancel_ticket_by_user`), organizer cancel
(`admin_cancel_ticket`), offline cancel (`cancel_offline_ticket`), offline refund
(`mark_offline_ticket_refunded`), offline payment confirm and unconfirm (`confirm_ticket_payment`,
`unconfirm_ticket_payment`), check-in (`check_in_ticket`), the event-cancel sweep
(`refund_one_cancelled_event_ticket`), series-pass cancel (`cancel_held_pass`), the
`checkout.session.completed`, `payment_intent.canceled` and `charge.refunded` webhooks, and the
reclaim routes (`cleanup_expired_payments`, `cancel_pending_checkout`, `release_uses_of_pending_tickets`).

Rules for new code:

- Lock `Payment` rows with `select_for_update(of=("self",))` and `.order_by("pk")`.
- Lock tiers and then tickets with `ticket_service.lock_tier_then_ticket(queryset)`. It uses
  `of=("self",)` and pk order, and it reads the tickets again under the lock.
- Do not use `select_for_update()` without `of=` on a query with `select_related`. Postgres then
  locks every joined row (`Ticket`, `Event`, `Organization`).
- Do not read a ticket from `select_related("ticket")` on an unlocked `Payment` query and then
  write it. Read it again under its lock.

`ticket_number_service.assign_ticket_numbers` locks the issued tickets and then the
organization's `TicketNumberSequence`, and the sequence lock is held until commit. So
any path that also locks tiers must lock them **before** it issues (numbers) tickets, and bulk
issuers call `day_membership_service.on_tickets_issued` **before** `assign_ticket_numbers`
(the `post_save` receivers run in that order too).
Checkout already does (`create_batch` locks tiers, then writes tickets). The series-pass
paths call `backfill_missing_tickets` (which locks tiers) before activating the pass's
pending tickets, both in `confirm_held_pass_payment` and in the Stripe
`checkout.session.completed` handler (`_activate_series_passes` runs before the
payments loop). Reversing either would let two transactions wait on each other.

`DiscountCode` rows are locked **after the tiers**: `apply_discount` runs after the tier lock and
the ticket inserts in checkout, so every path that gives uses back
(`discount_code_service.release_discount_uses`: cancel/expiry of an unfinished checkout) must lock
the codes only after the tiers it touches, in pk order. Derive the ids only after the Payment rows
(batch routes) or the ticket row (single cancel) are locked, filtered to PENDING tickets. The
cascade-delete helper `release_uses_of_pending_tickets`
locks Payment, then TicketTier, then the candidate Ticket rows (pk order), re-queries which are
still PENDING, and only then releases. Call it once after the whole tier loop (as
`handle_payment_intent_canceled` does) rather than interleaved per ticket.

Don't do network I/O after numbering: the per-org sequence lock serializes ticket
issuance for that organization until the transaction commits.

## Row locks across Stripe calls (refund paths)

Unlike the reserve/session split above, the single-refund paths deliberately do **not**
split the request. `refund_service.issue_refund` — and every caller: the organizer refund
endpoint (`refund_ticket_payment` → `issue_refund_for_ticket`), cancel-with-refund
(`admin_cancel_ticket`), user self-cancel (`cancellation_service.cancel_ticket_by_user` →
`_refund_if_required`), the event-cancel sweep and series-pass cancel
(`series_pass_service.cancel_held_pass`, looping per ticket) — calls `stripe.Refund.create`
inside a transaction (the `ATOMIC_REQUESTS` transaction for request paths). That is accepted
for one reason: the locked rows are small and belong to one payment.

**The rule.** Across a Stripe call, hold only:

- the `Payment` row (`select_for_update(of=("self",))`, pk order),
- its new `Refund` rows,
- the `HeldSeriesPass` row (series-pass cancel only).

Never hold a `TicketTier`, `SeriesPass`, `Event`, `Organization` or `Ticket` row across the call.
`issue_refund` locks with `of=("self",)`, so Postgres does not lock the joined `Ticket`, `Event` and
`Organization`. Each cancel path does its work in two steps:

1. Lock the `Payment` (and `HeldSeriesPass`). Read the ticket status. Call Stripe.
2. After Stripe returns, lock the `SeriesPass`, the tiers and the tickets
   (`ticket_service.lock_tier_then_ticket`). Check the state again. Change the status and
   release capacity (`quantity_sold__gt=0` and `F() - 1`).

**If step 2 finds the ticket changed** (for example a check-in during the Stripe call), two paths
handle it, in different ways. Do not try to undo the Stripe refund in either: the
`charge.refunded` webhook later records it as refunded-not-cancelled.

- `cancel_ticket_by_user`: step 2 runs in a savepoint. On a changed ticket the savepoint rolls
  back, the `Refund` row stays, and the path logs the structlog warning
  `cancel_rolled_back_after_refund`. On any other error in step 2 it logs the same warning and
  re-raises, so the whole transaction rolls back. A retry replays the same idempotency key.
- `cancel_held_pass`: no savepoint. A ticket that changed during the Stripe call is left as it
  is, the warning is logged, and the other tickets still cancel. Any other error rolls back the
  whole transaction, with no log.

`admin_cancel_ticket` holds the Payment lock throughout. The ticket cannot change under it after
the refund, because every writer that sets a ticket to CANCELLED takes the Payment lock first.
The event-cancel sweep refunds and cancels in separate transactions.

**Series-pass session expiry.** `cancel_held_pass` calls `stripe.checkout.Session.expire` right
after it locks the `HeldSeriesPass`, before the `SeriesPass`, tier and ticket locks. Only the
`Payment` and `HeldSeriesPass` rows are locked then, so the rule above allows the call. It runs
inside the transaction, not in `on_commit`: a failed earlier `on_commit` callback would skip it
and leave the session payable. `_expire_stripe_session` logs Stripe errors and does not raise.

**Why a refund in a transaction is acceptable.** The idempotency key
(`refund:{payment.pk}:{sequence}:{amount}`) is deterministic per attempt, so a retry after a
post-Stripe DB failure (rollback) replays the same key and gets back the same Stripe refund
object instead of double-refunding. The `charge.refunded` webhook self-heals a lost
response regardless of whether a retry ever happens — it moves a `Refund` row PENDING → SUCCEEDED
(or → FAILED) via the `refund_id` metadata, according to the Stripe refund status, and it never
re-cancels or re-decrements. The blast radius of
one slow Stripe call is one payment intent. The worst realistic collision is that intent's own
`charge.refunded` webhook delivery blocking for one round-trip behind the request that issued the
refund — and Stripe retries webhook deliveries, so a blocked delivery is not lost, only delayed.

**Refund status and the row lock (#1507).** The refund webhooks (`charge.refunded`, `refund.updated`,
`refund.failed`) lock the intent's `Payment` rows with `select_for_update(of=("self",))` and
`.order_by("pk")`, and nothing else. A `refund.*` event carries the refund object, so that handler makes
no outbound Stripe call at all. A `Refund` row moves only forward: PENDING → SUCCEEDED, and PENDING or
SUCCEEDED → FAILED. FAILED is final, so a stale or reordered event cannot undo a later one. A late
failure of a SUCCEEDED row sets the `Payment` back from REFUNDED to SUCCEEDED with a queryset
`update()`. Do not use `save()` for that move: the `Payment` signal would send the buyer a payment
confirmation. The forward move (to REFUNDED) uses `save()` on purpose, because it sends the buyer
the "refunded" email. The membership handler (`_handle_subscription_refund_object`) locks the `MembershipPayment`
first and lists the charge's refunds **under** that lock, like the `charge.refunded` membership path. A failure
that commits meanwhile waits for the lock, so the list is never older than the last committed failure. The cost
is one Stripe call while one `MembershipPayment` row is locked.

On the webhook side, `stripe_webhook_refunds`'s `_resolve_refunds` (the `Refund.list` fallback for
API versions where `charge.refunded` doesn't embed the refunds list) is called **before** any
`Payment.select_for_update()` — the call site's own comment states why: holding the lock
across that outbound call "would block concurrent user-initiated cancels
(`cancellation_service` locks the same Payment rows) for its duration." The advisory balance
preview (`build_event_refund_preview`, which calls `stripe.Balance.retrieve`) takes no row
locks at all — it is read-only and racy by design.

**Late payments (#1459).** A payment that completes after its ticket was cancelled keeps the ticket
`CANCELLED`. The `checkout.session.completed` handler marks the payment "refund due" (`SUCCEEDED`,
`refund_status` `PENDING`, no live `Refund` row) with a queryset update, so no payment confirmation is
sent, and logs `stripe_session_completed_cancelled_ticket_refund_due`. After commit it queues
`events.refund_late_payment`, which re-checks the marker and the cancelled ticket under the Payment
lock and refunds the full amount with `source=late_payment` (the platform fee is kept). The credit note
follows from the `charge.refunded` webhook, also for series passes. When the refund is processed
before the invoice exists, the invoice task credits it if the invoice (or the skipped-invoice record)
shows the payment (#1484). A late payment is refunded in full. The payment becomes `REFUNDED` only when the
`charge.refunded` webhook processes the refund. If that happens before the invoice task reads the
payments, the payment is not invoiced, and it gets no invoice and no credit note. Otherwise it is
invoiced, and the credit note follows. `cleanup_expired_payments`
re-queues a marker older than 10 minutes. A transient Stripe error (connection, rate limit, 5xx)
keeps the marker, so the sweep retries, for up to 6 hours. Any other Stripe failure, or a transient
one after 6 hours, leaves a `FAILED` `Refund` row, `refund_status` `FAILED` and the error log
`late_payment_refund_failed`, which needs an alert. A refund that the organizer issues by hand, also a
partial one, ends the automatic refund: the organizer took over the payment.

**Completed sessions with no Payment rows (#1489).** A `checkout.session.completed` for a charged
session with no `Payment` rows (deleted with the account or the event, or swept) still logs the ERROR
`stripe_session_paid_without_payments`, now with `organization_id` and `refund_eligible`. When the
session is a Revel ticket or series-pass checkout with a payment intent, the handler queues
`events.refund_orphaned_checkout_session` after commit, and the on-commit callback logs
`orphaned_checkout_refund_queued`. The task holds no lock and writes no row: it refunds the full payment
intent with the idempotency key `orphaned-checkout-refund:{session_id}` (the platform fee is kept) and
the refund metadata `revel_orphaned_checkout_session={session_id}`. A transient Stripe error is retried 6
times with backoff (about one hour). A terminal error, or the last transient one, logs the ERROR
`orphaned_checkout_refund_failed`. `charge_already_refunded` is a no-op. The status of the created refund
decides the log event, at ERROR so staff see every automatic refund: `succeeded` logs
`orphaned_checkout_refunded`, `failed` or `canceled` logs `orphaned_checkout_refund_failed`, and any other
status (`pending`, `requires_action`) logs `orphaned_checkout_refund_pending`. A pending refund can fail
later (for example, when the connected account has too small a balance). Then the `refund.failed` webhook
(or `refund.updated` with status `canceled`) of a refund with that metadata, with no `Payment` row for its
payment intent, on an account of an organization, logs the ERROR `orphaned_checkout_refund_failed` with
the ids and the `failure_reason`. A refund that is already `failed` when the task gets it can log this
event twice (task and webhook); there is no marker to stop that. Only `orphaned_checkout_refund_failed`
needs an alert. After a deploy
that adds these two event types, run `provision_stripe_webhooks --sync`.
There is no durable marker (no row to hold it): a lost dispatch increments
`revel_orphaned_checkout_refund_dispatch_failed` and logs the ERROR
`orphaned_checkout_refund_dispatch_failed`, and staff refund by hand. The task runs in Celery, so its
failure is not a Prometheus metric (Celery exports none); it alerts through the Loki rule on its log
event. The replay path never refunds: a duplicate delivery can arrive after the rows of a confirmed sale
were deleted with the account.

Organizations use Standard Connect accounts, so the Connect webhook also gets sessions that other
software created on those accounts. A session counts as Revel's only when all of these are true:

- `mode=payment` and the full metadata shape that `stripe_service.py` sets, with UUID ids (also each
  entry of the comma-separated `ticket_ids`): a ticket
  checkout has `ticket_ids`, `event_id`, `user_id` and `batch_size`; a series-pass checkout has
  `held_pass_id`, `user_id` and `ticket_ids`.
- The Stripe account belongs to an organization (`stripe_account_id` is unique, also for the platform
  account, so the account identifies at most one organization).
- When the parent row still exists (the event, or the `HeldSeriesPass`), it belongs to that
  organization. When it is gone, the account alone identifies the organization.
- The `success_url` starts with `SiteSettings.frontend_base_url` (where Revel builds every checkout
  return URL, #1153) or an active custom-domain origin of the organization. A session that fails this
  check is not refunded and logs `orphaned_checkout_refund_skipped_foreign_return_url`.

**Mitigation:** `STRIPE_HTTP_TIMEOUT_SECONDS` (`revel/settings/stripe.py`, default 15,
configured on `stripe.default_http_client` by
`common/service/stripe_config.py::configure_stripe`, which every module making an outbound
stripe-python call invokes at import) bounds stripe-python's own HTTP timeout,
which otherwise defaults to ~80s. `ATOMIC_REQUESTS` (see above) keeps the request's DB
transaction — and with it the row lock and the PgBouncer connection it's pinned to for the
transaction's lifetime (see the transaction-pooling note above) — open for as long as the
slowest outbound call inside it takes; bounding that call to 15s bounds the whole window to
15s instead of stripe-python's ~80s default.

## Plan row lock: FK `KEY SHARE` does not serialize inserts (#1427)

`update_plan` (currency/cadence guard) locks the plan row `FOR UPDATE` and then looks for live
subscriptions. It cannot rely on the foreign key from a concurrent `MembershipSubscription`
insert to block it: Django creates FK constraints `DEFERRABLE INITIALLY DEFERRED` on PostgreSQL,
so the `FOR KEY SHARE` that conflicts with `FOR UPDATE` is only taken at commit, after
`update_plan` has already read "no subscribers" and changed the cadence. Subscription creation
must therefore lock the plan row itself in `ensure_plan_sales_capacity`: `FOR NO KEY UPDATE` when the
plan is capped (it counts slots; it serializes capped creators and conflicts with `update_plan`'s `FOR UPDATE`, but not with the uncapped `FOR KEY SHARE`, so a plan that gains a cap mid-checkout cannot deadlock), `FOR KEY SHARE` when it is not. `KEY SHARE` locks don't conflict
with each other, so uncapped checkouts don't queue behind one another across the Checkout-session
Stripe call (held under `ATOMIC_REQUESTS`), but they conflict with `update_plan`'s `FOR UPDATE`. After taking the lock the helper refreshes the
caller's plan instance in place, so the rest of the creation uses the committed currency, cadence,
price, Stripe price and cap (the local sales-pause and active checks are not repeated after the lock: a checkout that passed them before a pause or archive committed carries on, and an archived plan's Stripe Price may then reject the session). The model-level guard in `clean()` also reads the
persisted row `FOR UPDATE`; that serializes with in-flight creations only when the save runs inside
a transaction (Django admin, `update_plan`), so shell edits need an enclosing `transaction.atomic()` to hold the lock through the write.
The threaded regression test is `events/tests/test_plan_guard_1427.py`
(`transaction=True`, two real connections).

## Backfills and rolling deploys

A data migration that backfills a column runs once, but during a rolling deploy the old pods keep inserting rows with the column default. Put the UPDATE in a plain idempotent function that the migration calls, and run it once after the rollout (for `Ticket.unconfirmed_guest`: `manage.py backfill_unconfirmed_guest --before <ISO time with offset>`, `--dry-run` first). Set the cutoff to the moment the last old web container stopped, not a loose "rollout finished" time: until then old containers still insert unflagged rows. Make the predicate match evidence that new rows rarely carry (here, a Payment row on an unflagged guest ticket), and still run it once: a later pass gains nothing and widens the blast radius of a predicate mistake. Know the residual: a guest who confirms by email after the tier became ONLINE gets a paid reservation with Payment rows, so such a ticket created before the cutoff is flagged too.

## Migration Squashing (one per app per PR, never hand-edit schema migrations)

Branch policy: a PR introduces at most **one schema migration per app** — iterative work
that accumulated several (tables, then a default tweak, then an `on_delete` change) gets
squashed before merge. Data migrations are the exception and stay separate: backfills and
"special" data migrations (beat-task `PeriodicTask` rows, seeded data) are hand-written by
design and must not be folded into schema migrations (`make nuke-db` preserves them for the
same reason).

**Never hand-edit a schema migration to squash it** — don't append `AddField`s to an
existing generated file or merge choices lists by hand. Hand-edited "generated" files drift
from what `makemigrations` would produce (operation ordering, fields inline in `CreateModel`
vs. trailing `AddField`s) and forfeit the guarantee that the migration is exactly the
autodetector's view of the models. Manual authorship is for data migrations only. Squash by
deleting and regenerating:

1. **Delete** the branch's schema migrations for the app (all of them, including the base
   one being squashed into).
2. **Data migrations in between?** They reference the deleted schema migration **by
   filename string** in `dependencies` (e.g. `("events",
   "0101_memberships_subscriptions_integration")`). Temporarily move them out of the
   migrations dir, regenerate with the *original* name so the reference still resolves —
   `manage.py makemigrations <app> --name <original_name>` — then move them back. Editing
   their `dependencies` line instead is acceptable (it's a data migration), but keeping the
   filename is less churn.
3. **Regenerate**: `make migrations` (or the `--name` form above). Verify with
   `makemigrations --check --dry-run` (no changes detected) and `git diff` — expect the
   squashed fields inline in `CreateModel` for new models and canonically-ordered
   `AddField`s for existing ones.
4. **Local DB bookkeeping**: if the pre-squash migrations were already applied locally,
   there is no need to roll back (the schema is identical). Remove the stale
   `django_migrations` rows for the now-deleted files with
   `manage.py migrate <app> --prune`. Any other machine that had applied them does the
   same after pulling. **Only if it had applied the whole pre-squash chain**: a DB that ran
   the base migration but not the later ones sees the regenerated base as applied and never
   gets the missing columns, and pruning does not add them. Check the old head is applied
   (`showmigrations <app>`) *before* pruning; if not, finish the old chain on the previous
   code first (or roll the app back below the base and re-migrate after pulling).

## `oauth.0001` `run_before` (a documented hand edit)

`src/oauth/migrations/0001_initial.py` carries a line the autodetector did not write:

```python
# Documented exception to "never hand-edit schema migrations" (spec §6.4): DOT's
# 0001_initial has no dependency on the swapped Application model. Re-add after any
# regeneration (make nuke-db / make restart). Guarded by oauth/tests/test_migrations.py.
run_before = [("oauth2_provider", "0001_initial")]
```

`oauth.OAuthApplication` is the swapped `OAUTH2_PROVIDER_APPLICATION_MODEL`, and
django-oauth-toolkit's own `0001_initial` creates `AccessToken`, `RefreshToken`, `IDToken` and
`Grant` with FKs pointing at it — but it declares **no dependency** on the swapped app, because
DOT cannot know which app that will be. On a from-scratch `migrate` the planner is then free to
run DOT's initial first, and the FK columns come out typed from Django's fallback rather than
from our model's **UUID** primary key. `run_before` is what forces the order.

Two consequences:

- **Re-add it after any regeneration.** `make nuke-db` and `make restart` delete and regenerate
  migrations; the line is not reproduced. So does a squash (delete the files, then
  `makemigrations oauth --name initial` so `0002_add_oauth_periodic_tasks.py`'s
  `("oauth", "0001_initial")` dependency still resolves).
- **Two tests guard it**, and both must pass against a database migrated from scratch
  (`pytest --create-db`), never an incremental one:
  `oauth/tests/test_migrations.py::test_oauth_initial_runs_before_dot_initial` reads the
  forwards plan, and `test_dot_token_tables_take_the_uuid_application_key` asks
  `information_schema` whether `application_id` is `uuid` on all four DOT token tables — which is the property the ordering exists to produce.

## Two token worlds: `blacklist_user_tokens` also revokes DOT tokens

There are two independent credential systems: ninja-jwt session tokens
(`OutstandingToken`/`BlacklistedToken`) and django-oauth-toolkit app tokens (`AccessToken`,
`RefreshToken`). `accounts.jwt.blacklist_user_tokens` invalidates **both**, and the OAuth half
is deliberately *not* gated on `oauth.utils.oauth_provider_enabled()`: the credential-presence
feature flag (ADR-0008) governs whether app tokens can be *issued*, not whether a ban is
honoured. Gating it would leave stale rows alive across a disable → ban → re-enable window, and
would make `ScopedJWTAuth`'s `user.is_active` check the only guard instead of a backstop.

Two things follow that are easy to miss:

- **The import is lazy** (`from oauth.service import token_service` inside the function) because
  `oauth` imports `accounts` — a module-level import is a circular one. `oauth` is
  unconditionally in `INSTALLED_APPS`, so the import itself is always safe.
- **The blast radius is wider than "sessions".** `accounts.service.account` calls this on email
  rotation, so changing an email address disconnects every connected app. A *global ban* goes
  further still (`global_ban_service.deactivate_user_for_ban`, deliberately not here): it also
  deactivates the OAuth applications the banned user *owns* and revokes their tokens, which cuts
  off every other user of those apps. That placement matters — doing it in
  `blacklist_user_tokens` would disconnect an app's whole user base because its developer
  changed their email address.

The return value counts session JWTs only; OAuth revocations are logged separately
(`user_oauth_credentials_revoked`) because they are a different credential with a different
lifetime and callers reading the number mean sessions. "Session JWTs" includes the refresh tokens
issued by rotation, which `accounts.service.token_refresh` records as `OutstandingToken` rows, so
the count covers refreshed sessions too (see the next section).

## Refresh rotation, lineage logout and reuse grace

`POST /api/auth/refresh` and `POST /api/auth/logout` are served by `accounts/service/token_refresh.py`,
not by ninja-jwt's stock refresh. The invariants that are easy to break:

- **Every refresh token belongs to a lineage (one login).** The lineage id is the `sid` claim, or the
  `jti` of the login's first token (which has no `sid`). Rotation stamps `sid` on what it issues and
  records the new token as an `OutstandingToken`; without that row `blacklist_user_tokens` and logout
  could not find the session.
- **The user row is locked** (`select_for_update(no_key=True)`) by rotation, grace, logout and
  `blacklist_user_tokens`. That is what stops a logout or ban from missing a token minted at the same
  instant. `NO KEY UPDATE` self-excludes but does not block inserts of rows that reference the user.
- **Reuse grace** (`REFRESH_TOKEN_REUSE_GRACE_SECONDS`, default 30, `0` = strict) lets a just-rotated token
  be exchanged once more. It depends on the cache (`refresh_rotated:<jti>` marker, single-use
  `refresh_grace_used:<jti>` key) and fails strict when the cache is down. Only rotation writes the
  marker, and a lineage with no live token left never qualifies, so grace cannot revive a logged-out,
  reset or banned session.
- **Logout** revokes the presented token's lineage only; a stale, already-rotated token still logs its
  lineage out.
- **Raise outside the atomic block** when a tenant session is dead: the blacklist write must commit even
  though the request answers 401.

See [Authentication & Sessions](architecture/auth-sessions.md) for the model as a whole.

## User-facing error strings: `make i18n-literals`

`scripts/check_i18n_literals.py` (run by `make i18n-literals`, which is part of `make check` and CI)
rejects an untranslated message in `raise HttpError(<status>, msg)` and in `raise <AppError>(msg)` for any
exception mapped with `make_simple_handler`, subclasses included (dispatch is by MRO). Write
`str(_("..."))`; for interpolation translate first, then `.format(...)`. f-strings are rejected outright
because `xgettext` cannot extract them. See [Controllers](architecture/controllers.md#user-facing-error-strings).

## Negative security tests: prove the request was *accepted* first

A test that asserts "the admin could not change X" passes trivially whenever the attempt fails
for a reason that has nothing to do with the control under test. On this branch, one such test
passed against a **fully vulnerable** admin twice before anyone noticed:

1. First because the POST body left a `SplitDateTimeField` unsplit, so the ModelForm rejected it
   with "this field is required" — the row was never written, and "the value did not change"
   was true for the wrong reason.
2. Then because the forged row was re-read as a naive datetime in `TIME_ZONE`, which put it in
   the past, so the token was expired by the time it was used — refused by expiry, not by the
   readonly guard.

The implementer's own summary is the rule: *a body the form rejects makes "the admin could not
change X" pass for the wrong reason.*

So a negative test must first assert that the request was **accepted** and only then assert the
negative:

- For a Django admin change view, assert `302` (a `200` is the form re-rendered with errors) and
  only then re-read the row and assert the field is unchanged.
- For an API route, assert the status you expect from the control being tested — `403`, not
  "anything other than 200" — and never `!= 403`, which a `404` from a renamed path satisfies.
- Construct the attack payload against the *vulnerable* code first and watch the test go RED. A
  negative test that has never been red is a test of nothing.

## Admin responses that carry a person: `test_holder_profile_guard.py`

A new admin route that nests `MemberUserSchema`, `MinimalRevelUserSchema` or `BlacklistEntrySchema`
(directly or through any component that embeds them) fails `events/tests/test_controllers/test_holder_profile_guard.py`
until it is classified. Add it to `HOLDER_PROFILE_OPERATIONS` only when its subject acted toward
the organization (member, applicant, waitlist, pass holder...). When the row can be
staff-originated (box office, staff RSVP, invitation), return `StaffViewUserSchema` instead and
annotate the queryset with `annotate_holder_known` (one query for the list; see ADR-0020). The
same predicate gates staff actions on an account (`require_known_holder`). A blacklist entry never
creates an `OrganizationMember` row for a non-member, so code that must exclude banned people checks
`get_hard_blacklisted_user_ids` / `check_user_hard_blacklisted` as well as `banned_memberships()`.

## Key rotation (`SECRET_KEY`, `SALT_KEY`)

Sign with `common.keys.signing_key()` and verify with `common.keys.decode_jwt(..., verification_keys())`;
never read `settings.SECRET_KEY` directly (a test enforces this). The `*_FALLBACKS` lists are plain
comma-separated (newest first, no quoting; `#`, quotes and backslashes are kept verbatim).
`SALT_KEY` itself is never comma-split.

Rotate in two phases, because during `docker rollout` old and new containers run side by side:

1. **Introduce.** Keep the old key primary and add the NEW key to `SECRET_KEY_FALLBACKS` /
   `SALT_KEY_FALLBACKS`. Roll out. Every container can now read both keys; nothing writes with the new one.
2. **Promote.** Make the new key primary and put the old key first in the fallbacks. Roll out.

Then run `manage.py rotate_encrypted_fields` (`--dry-run` first), then `--verify` (decrypts with the
primary key only; non-zero exit on any failure), and drop the fallbacks after the grace window
(refresh tokens: `REFRESH_TOKEN_LIFETIME_DAYS`).

Why two phases: `EncryptedFieldMixin` swallows `InvalidToken` and returns the raw ciphertext, so an old
container that cannot decrypt a row written under the new key reads garbage and saves it back,
corrupting it permanently. **Never remove a key that has ever been primary until the data is
re-encrypted.**

Unsubscribe links sit in mailboxes for years (up to `UNSUBSCRIBE_TOKEN_LIFETIME_DAYS`, 10 years), so give
them their own list before dropping `SECRET_KEY_FALLBACKS` (`manage.py check` warns with `common.W006`
until set). It is also rolled out in two stages, because during a rolling deploy new containers would
otherwise sign links that old containers reject:

1. **Introduce:** `UNSUBSCRIBE_SIGNING_KEYS=<current key>,<new key>`. Containers keep signing with the
   current key while every container learns the new one. When pinning for the first time, the current key
   is the current `SECRET_KEY`.
2. **Promote**, once every container has that list: `<new key>,<old key>,...older keys`. Keep every older
   key until its links have expired; never replace the list.

`SALT_KEY` is read verbatim but `SALT_KEY_FALLBACKS` is comma-separated: a primary salt containing a comma
triggers `common.W007` and must be re-encrypted under a comma-free salt before it is rotated out.

`EncryptedFieldMixin.keys`/`.f` are `cached_property` on the shared field instance: tests that
switch key settings must pop them from `field.__dict__`.

## After-commit dispatch that must not fail the request (`robust=True`)

An `on_commit` callback runs after the request transaction commits. If its `.delay()` raises (broker
down), the data is already stored but the request answers 500, and the task is never queued. When that
work must not fail the request, use a named callback that logs its own failure, register it with
`transaction.on_commit(callback, robust=True)`, and add a sweep that finds the work again from the
database. Examples: `account_deletion_dispatch` in `accounts/tasks/gdpr.py`, and `_scan_dispatch` in
`common/service/upload_service.py` with the `rescan_pending_uploads` sweep (#1446), and the series
pass holder materialization with the `reconcile_series_pass_tickets` sweep (#1536).

Never pass a `functools.partial` with `robust=True`: Django logs a failing robust callback by its
`__qualname__`, and a `partial` has none, so the error path itself raises `AttributeError`.
## Account erasure: keep the holdings, erase the person

`accounts.tasks.delete_user_account` is the one deletion path (self-service confirm, the unverified
sweep, the admin delete). After it locks the account's checkouts (`release_capacity_of_account`), it
reads `kept_holdings` under those locks:

- ACTIVE or CHECKED_IN tickets whose event `end` is at or after `now - 120 days` (full datetimes).
- Every ACTIVE held series pass, also one whose series has no event yet. (PR 3's retention job deletes
  it 120 days after its last covered event, and only when the series has no upcoming event.)
- The held pass of every kept ticket (`Ticket.held_pass` is `RESTRICT`).

No kept holdings: `user.delete()`, as before. Otherwise `account_erasure.erase_account_keeping_holdings`
runs in the same transaction:

1. Copy the holder email onto the kept tickets (`holder_email`) and the holder name and email onto the kept
   passes (`holder_name`, `holder_email`). A ticket keeps a `guest_name` that is already set. A ticket with a
   blank `guest_name` gets the account name only when its organization knows the holder
   (`holder_visibility.is_known_holder_id`): staff saw that name before the erasure. For any other holder (an
   unconfirmed guest checkout without a ticket name) staff saw only the email, so the name stays blank and the
   staff view shows the kept email. Clear `raw_response` (the stored Stripe payload) of the kept
   payments; keep `buyer_billing_snapshot` for the retention period (a late invoice or credit note needs it).
   A later Stripe event (a refund, a late completion, a cancelled intent) stores `{}` for an erased buyer:
   every writer of `Payment.raw_response` goes through `erased_holder.stripe_payload_to_store`.
2. `blacklist_user_tokens`, before the cascade nulls `OutstandingToken.user`.
3. Walk every relation to `RevelUser` (`get_candidate_relations_to_delete`, so hidden M2M through rows
   too) and give each the outcome of `user.delete()`: CASCADE rows deleted, SET_NULL columns nulled,
   PROTECT rows refuse. The exceptions are the kept tickets, passes and their payments, and
   `NotificationPreference` (kept and **not** silenced: the event-cancel audience skips silenced accounts,
   and the dispatcher already drops every type but the five cancellation and refund emails). Tickets go first because of the `RESTRICT`. If a kept row
   is gone afterwards, the task raises and the transaction rolls back.
4. Anonymise the row: placeholder `username`/`email` (`erased-<id>@erased.invalid`), names, phone, bio,
   pictures, Stripe Connect, TOTP and `last_login` cleared, unusable password, `is_active`/`is_staff`/
   `is_superuser`/`guest`/`email_verified` False, `credentials_changed_at` and `erased_at` set.

`gdpr.purge_user_history` then runs as for a full delete. On both paths the stored data-export ZIP of the account
is deleted after commit (`account_erasure.delete_data_export_file_after_commit`): the export row cascades, but a
row delete does not delete the file, and its emailed link stays valid for 7 days. A repeated export deletes the
ZIP of the earlier one after commit, and an export that fails after it wrote its ZIP deletes that ZIP at once
(`gdpr._stale_export_cleanup`), so the export row always names the only export file of the account.

An export file can still lose its row: the export task writes the ZIP inside the transaction that holds the
account lock, so a worker that is killed (the 300 s time limit, an OOM kill) after the write and before the commit
rolls the row back and leaves the file. The beat task `accounts.sweep_orphan_data_exports` runs once a day (03:40
UTC, seeded by migration `accounts.0041`). It lists the upload folder of `UserDataExport.file`
(`protected/user_data_exports/`) through the storage API and deletes each file that has the name of an export ZIP
(`revel_export_<account id>*.zip`; each export has its own name, `revel_export_<account id>_<16 hex>.zip`, so a
signed link to an earlier ZIP never opens a later one), that no row names, and that is older than the lifetime of a download link
(7 days, `ORPHAN_DATA_EXPORT_MIN_AGE`). The age limit keeps an export that is being made now. One sweep deletes at
most 500 files, and its log has counts only. It also removes the files that earlier versions left without a row in that folder.

Sign-in and a failed deletion:

- The self-service confirm runs one UPDATE on an **active** account: `is_active=False` and
  `deletion_requested_at=now`. An account that was already inactive (the unverified sweep, an admin) gets no mark.
- The run carries its mark: the confirm passes the timestamp to the task (`deletion_mark`, ISO 8601). A run for
  an account that got no mark carries `None`, and so do the admin runs and the unverified sweep.
- One run for each mark: a second confirmation of an account that already has the mark queues nothing. Two runs
  would share the mark, and a failure of one would give the access back while the other still deletes the account.
  An account without a mark (already inactive) queues a run for each confirmation; none of them can reactivate.
- The dispatch is a robust `on_commit` callback. If `.delay` raises (broker down), it logs
  `account_deletion_dispatch_failed` (ERROR, user id only). The account stays inactive with its mark, because the
  response already said that the deletion will run. The stale-mark sweep (below) queues the run again.
- On the task's final failure (any exception raised in the task on any path, also the soft time limit,
  or a referral payout still in flight after the last retry), it logs `account_deletion_failed` (ERROR, user id only). Then one UPDATE sets `is_active=True` and
  clears the mark, but only for a row that still has exactly the mark of that run, has no `erased_at`, still has
  the email it was read with, and whose email is not globally banned
  (`account_erasure.restore_access_after_failed_deletion`). After that UPDATE has committed, the ban of the
  email is checked once more in a new statement (`global_ban_service.deactivate_restored_account_if_banned`):
  a banned account goes back to the state of a refused restore (inactive, with its mark, so the stale-mark
  sweep queues the deletion again), with no ban notification, and `account_restore_undone_for_ban`
  (WARNING, user id only) is logged. The check is a robust on-commit callback: its own error is logged (ERROR,
  by Django, with the callback name `check_ban_after_restore`) and does not replace the error of the deletion.
  Known limit: a check that fails before its UPDATE (a database error in that short window) leaves the account
  as restored, also when its email is banned. The UPDATE cannot see a domain ban that commits while it runs, and the
  task of a domain ban deactivates only accounts that are active when it reads them; with the second check, one
  of the two always sees the other. A run without a mark never restores, so it cannot
  clear the mark of a later confirmation.
- A run with a mark is fenced (`gdpr._mark_changed`): it works only while the account is inactive and still has
  exactly the mark of that run. The task reads the account again and checks this at three points: immediately
  before the subscription cancel, immediately before the referral cleanup, and under the account row lock,
  before it releases capacity and erases or deletes. When a check fails, the run logs
  `account_deletion_skipped_mark_changed` (INFO, user id only) and ends: no further step, no restore, no retry.
  The lock is held for the last phase only. The cancel and the cleanup run without it (statement PDF writes,
  the locks of the payout task, and the cancel must commit on its own), so a change that commits after the
  check of one of these two steps does not stop that step; every later step is skipped (by design, see
  `.claude/review/by-design.md`). So an old run cannot delete an account that got its access back, that an admin
  reactivated, or that has a newer run. A run without a mark has no fence.
- The admin delete, the "Force full delete" action, an admin who changes `is_active`, and a ban clear the
  mark. So an admin run never reactivates, also when a self-service run is pending. Because the fence then
  stops the run of the confirm, each of these decides what happens next:
  - The admin delete and "Force full delete" queue their own run without a mark.
  - A ban (`global_ban_service.deactivate_user_for_ban`, both branches) reads the mark under the row lock in
    the transaction that clears it. When a mark was set, it queues one run without a mark on commit
    (`gdpr.account_deletion_dispatch`, the helper of the admin delete). The banned account is still deleted or
    erased, and a failure of that run leaves it inactive. A domain ban deactivates active accounts only, so a
    pending account in that domain keeps its mark and its own run.
  - No code may write `is_active` or the mark from data that it read earlier without a lock. Save a `RevelUser`
    with `update_fields` that name only the fields that changed: a full `save()` of an instance that was read
    earlier would write the old `is_active` and mark back, and the fence would then stop the deletion for good.
  - An admin who changes `is_active` cancels the deletion: ticked, the account is active again; unticked (only
    possible with a form that was opened before the confirm), the account stays inactive and undeleted.
- An erasure clears the mark; a full delete removes the row.
- The stale-mark sweep: the beat task `accounts.requeue_stale_account_deletions` runs every 15 minutes (seeded by
  migration `accounts.0040`). It covers a run that never reports: a failed dispatch, a hard kill of the task (the
  300 s time limit, or an OOM kill of the child process; Celery acks the message: a time-limit kill acks by
  default, and `task_reject_on_worker_lost` is off for a lost child process, so there is no retry) and a message
  lost by the broker. A lost whole worker is redelivered (`acks_late`). The sweep selects inactive rows without
  `erased_at` whose mark is older than 24 hours (`STALE_ACCOUNT_DELETION_AFTER`). One run lives about 11 hours at
  most: 12 retries with a backoff capped at 1 hour, and the 300 s time limit for each attempt. For each row a
  compare-and-set UPDATE (`WHERE deletion_requested_at = <the value read>`) writes a new mark, and the new run
  carries the new mark. So two sweeps queue one run, and a late failure of the old run cannot give the access
  back. If the dispatch fails, the sweep puts the old mark back and raises, and the next sweep tries again. One
  sweep takes at most 100 rows, oldest mark first. Rows without a mark (admin, ban, the unverified sweep) are
  never selected. An operator can still reactivate a pending account in the admin (ticking `is_active` also
  clears the mark).
- Rolling deploy: the column is nullable, so old code keeps working. A task queued before the deploy comes
  from a confirm that did not deactivate the account and carries no mark, so its failure changes nothing. A new
  confirm whose task an old worker receives fails there (the old task does not accept `deletion_mark`) and
  deletes nothing; the account stays inactive with its mark, and the sweep queues the run again after 24 hours.

No `Notification` row is stored
for a type an erased account never receives, and the digest sweep skips erased accounts (it reads `erased_at` again
for each account before it sends). A queued data export is skipped for an erased account and while a deletion is
pending; the export holds the account row lock (also across its emails; each email is sent in its own savepoint, so a
database error in its `EmailLog` write does not roll back the FAILED or READY status of the export), so an erasure cannot commit in the
middle of it, and the account's checkouts, sign-in and token refresh wait for it. A second run on an erased account with
holdings does nothing; the superuser admin action "Force full delete" passes `force_full_delete=True`.

Rules for new code:

- A new relation to `RevelUser` fails `events/tests/test_account_erasure/test_relations.py` until you
  record its outcome there. If the erasure must keep its rows, add it to `account_erasure._kept_rows`.
- Kept models must not be history-tracked: the history purge would drop their history.
- Code that shows or emails a ticket or pass holder reads the snapshot through
  `events/utils/erased_holder.py` (`holder_email`, `ticket_holder_name`, `pass_holder_name`).
  `RevelUser.get_display_name()` returns "Deleted user" for an erased account, so an unpatched path
  never shows the placeholder. Notification emails greet with `recipient_name`, not `user.display_name`.
- `holder_email` is a `CharField`, not an `EmailField`: it is a stored snapshot, and `full_clean` on a
  later save (check-in, cancel) must never reject it (the same rule as `MembershipRecord.email`).
- Links mailed before the erasure (password reset, email verification) find no account: their lookups
  exclude `erased_at` rows and rows with `deletion_requested_at`, as if the row were gone. So a link never
  reactivates an account whose deletion is pending, and the answer does not tell that it is pending. The
  senders (resend verification, password reset request, the admin verification action, a repeat signup) skip such accounts. An
  OIDC login does not convert or reactivate a pending guest (`convert_guest(reactivate=True)`).
- Never send mail to `user.email` of an erased account: the email backend guard drops it (see
  [Email Sending](architecture/email-sending.md#the-erased-recipient-guard)).

## Deletes that cascade pending checkouts (#1490)

Deleting an event, a ticket tier or an event series cascade-deletes its tickets and their `Payment`
rows. A PENDING payment is an unfinished checkout, so each delete service ends it first, inside
the same transaction and before the delete:

`pending_checkout.lock_for_cascade_delete(tickets, tiers=..., parents=...)` does these steps:

1. It locks all `Payment` rows of the tickets in one statement (`of=("self",)`, pk order). It reads
   the session ids of the PENDING rows, and of the FAILED rows whose session has not expired yet.
   (The event-cancel sweep marks a pending checkout FAILED and leaves its session open.)
2. It locks every tier that the delete removes (`FOR UPDATE`, pk order). A reservation locks its
   tier before it inserts, so after this lock no new `Payment` row can appear for these tiers.
3. It locks the parent rows that the delete removes (`FOR UPDATE`, pk order): the organization,
   the series, the events. A transaction that inserts a child row meanwhile waits at its commit-time
   foreign-key check and then fails. Without this lock, the delete failed on that row instead.
4. It locks and reads the `Payment` rows again (#1532). This catches the rows that a reservation
   committed between steps 1 and 2. Their sessions go into the same expire set.
5. `discount_code_service.release_uses_of_pending_tickets(tickets)` gives back the discount code
   uses. It locks Payment, then TicketTier, then Ticket, then DiscountCode.

After commit, a named closure expires each session at Stripe, per Stripe account (best effort,
`robust=True`). A rollback discards it.

Step 4 is out of the global lock order, but only for the rows that committed after step 1. If a
cancel, an expiry or a webhook holds such a row and waits for a tier of step 2, Postgres detects the
deadlock and stops one of the two transactions. A session that is then paid with no `Payment` row is
refunded in full (`refund_orphaned_checkout_session`, #1489).

A loop that runs a delete service once per row in one transaction (the admin bulk deletes of events,
tiers and series) first calls `pending_checkout.lock_for_bulk_delete` for the whole selection. It takes
steps 1 to 3 for all the rows at once: the payments, the tiers, then the parent rows, each in pk order.
Each service locks only the rows of its own delete, so the loop alone would not lock them in pk order.
One checkout can span tiers and events, and a series pass locks the tiers of several events in pk
order, so such a loop could deadlock with them. The bulk lock ends no checkout: each service in the
loop still expires the sessions of its own rows, so a series delete that fails in its own savepoint
expires nothing.

The services are `event_update_service.delete_event`, `ticket_service.delete_ticket_tier` and
`event_series_service.delete_event_series`. Call them, never `.delete()` on these models directly.
A tier delete still hard-deletes its issued tickets and their `Payment` and `Refund` rows.

The Django admin follows the same rule (#1499). Deleting an event, a tier or a series calls these
services. Deleting a ticket or an organization calls `pending_checkout.lock_for_cascade_delete`
directly (a ticket delete passes no tiers, so it runs only steps 1 and 5). The tier and series inlines have `can_delete = False`, because an
inline delete would call `.delete()` and skip the service. The admin also refuses to delete a
PENDING `Payment`, because its pending ticket would never be reclaimed. The guard is in
`PaymentAdmin.get_deleted_objects`, not in `has_delete_permission`: Django checks that permission for
every cascaded object, so it would also block deleting an event, tier or ticket that has a pending checkout.

## Independent commit callbacks: `on_commit_independent` (#1488)

Django runs the commit callbacks in sequence. A callback that raises stops the callbacks
after it, unless it is registered with `robust=True`. A failure after commit cannot roll
back data, so a dispatch that other dispatches follow must not stop them.

- Use `common.utils.on_commit_independent(callback, "<failure_event>", **ids)`. It logs a
  failure at ERROR with the ids, does not raise, and registers with `robust=True`.
- Outside an atomic block, the callback runs at once, and a failure is logged in the same way.
- Do not pass a `functools.partial` or a lambda directly to `on_commit(..., robust=True)`.
  Django logs `__qualname__`, which a `partial` does not have. The helper wraps the callback
  in a named function, so a `partial` is safe as its argument.
- For a loop, register one callback for each item, so one failed item does not stop the others.
- The Ticket `post_save`/`post_delete` receivers, the bulk-create side effects of
  `BatchTicketService`, `enqueue_waitlist_processing`, the offer notifications and the offer
  expiry loop use this pattern. Most other `on_commit` sites do not use it yet.
- Test it: make the first dispatch raise inside
  `django_capture_on_commit_callbacks(execute=True)`, and assert that the second still runs.
