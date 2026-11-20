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

**`Payment` (pk order) → `HeldSeriesPass` → `SeriesPass` → `TicketTier` (pk order) → `Event` → `Ticket` (pk order)
→ `DiscountCode` (pk order) → `MembershipRecord` → `TicketNumberSequence` (org-id order)**

**Ticket-ownership lemma.** Every transaction that touches an existing ticket that has a `Payment`
locks that `Payment` first. So, once a transaction holds the `Payment`, no other path can cancel,
activate or release that ticket. Check-in is the one writer that takes no lock. A path may trust
the ticket status only when it reads it in a statement that starts **after** it holds the `Payment`
lock, and never from the locking statement itself (for example through `select_related("ticket")`).
Under READ COMMITTED, a statement that waited for a row lock is re-checked only if the locked row
changed. A cancel locks the `Payment` but updates only the `Ticket`, `TicketTier` and
`DiscountCode`, so the waiting statement would keep the old ticket state. Re-check `CHECKED_IN`
after the ticket lock.

Paths that follow the lemma: user cancel (`cancel_ticket_by_user`), organizer cancel
(`admin_cancel_ticket`), offline cancel (`cancel_offline_ticket`), offline refund
(`mark_offline_ticket_refunded`), the event-cancel sweep
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
handle it: `cancel_ticket_by_user` and `cancel_held_pass`. In both, step 2 rolls back (a
savepoint), the `Refund` row stays, and the path logs the structlog warning
`cancel_rolled_back_after_refund`. Do not try to undo the Stripe refund. The `charge.refunded`
webhook later records the refund as refunded-not-cancelled. Series-pass cancel leaves such a
ticket as it is and continues with the other tickets. If step 2 raises another error, the
whole transaction rolls back (and the warning is logged). A retry then replays the same
idempotency key.

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
response regardless of whether a retry ever happens — it only finalizes a `Refund` row PENDING →
SUCCEEDED via the `refund_id` metadata, it never re-cancels or re-decrements. The blast radius of
one slow Stripe call is one payment intent. The worst realistic collision is that intent's own
`charge.refunded` webhook delivery blocking for one round-trip behind the request that issued the
refund — and Stripe retries webhook deliveries, so a blocked delivery is not lost, only delayed.

On the webhook side, `stripe_webhook_refunds`'s `_resolve_refunds` (the `Refund.list` fallback for
API versions where `charge.refunded` doesn't embed the refunds list) is called **before** any
`Payment.select_for_update()` — the call site's own comment states why: holding the lock
across that outbound call "would block concurrent user-initiated cancels
(`cancellation_service` locks the same Payment rows) for its duration." The advisory balance
preview (`build_event_refund_preview`, which calls `stripe.Balance.retrieve`) takes no row
locks at all — it is read-only and racy by design.

**Known follow-up.** A payment that completes after its ticket was cancelled keeps the ticket
`CANCELLED` (the `checkout.session.completed` handler skips the activation and logs
`stripe_session_completed_cancelled_ticket_skipped`). The captured money is not refunded
automatically yet.

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
