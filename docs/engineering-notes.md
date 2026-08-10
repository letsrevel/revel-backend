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
savepoint, never the lock (code comments in `subscription_service` /
`subscription_stripe_plan_change` state this explicitly). This is acceptable where the
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

## Row locks across Stripe calls (refund paths)

Unlike the reserve/session split above, the single-refund paths deliberately do **not**
split the request. `refund_service.issue_refund` — and every caller: the organizer refund
endpoint (`refund_ticket_payment` → `issue_refund_for_ticket`), cancel-with-refund
(`admin_cancel_ticket`), user self-cancel (`cancellation_service.cancel_ticket_by_user` →
`_refund_if_required`), and series-pass cancel (`series_pass_service.cancel_held_pass`,
looping per ticket) — takes `select_for_update` on the `Payment` row (`admin_cancel_ticket`
and series-pass cancel also lock the `Ticket`/`HeldSeriesPass` row) and holds it across one
`stripe.Refund.create` round-trip, inside the `ATOMIC_REQUESTS` transaction. That lock
releases only when the view returns, exactly the hazard the checkout split exists to avoid.

**Why this one is accepted instead of split:** the same three guarantees documented on
`cancel_ticket_by_user` apply to every caller. The idempotency key
(`refund:{payment.pk}:{sequence}:{amount}`) is deterministic per attempt, so a retry after a
post-Stripe DB failure (rollback) replays the same key and gets back the same Stripe refund
object instead of double-refunding. The `charge.refunded` webhook self-heals a lost response
regardless of whether a retry ever happens — it only finalizes a `Refund` row PENDING →
SUCCEEDED via the `refund_id` metadata, it never re-cancels or re-decrements. And the locked
rows are **user/payment-scoped**, not tier-scoped: the blast radius of one slow Stripe call is
one payment intent, not every buyer of a hot tier. The worst realistic collision is that
intent's own `charge.refunded` webhook delivery blocking for one round-trip behind the
request that issued the refund — and Stripe retries webhook deliveries, so a blocked delivery
is not lost, only delayed.

**Scope discipline that must be preserved** if this area changes: never let a *tier/inventory*
row join the locked set across the network call. Every cancel path refunds first and only
`F("quantity_sold") - 1`s the `TicketTier`/`SeriesPass` row afterwards, as a separate
lock-free `UPDATE` — that's what keeps this trade-off confined to one payment instead of
regressing into the tier-wide contention the reserve/session split exists to prevent. On the
webhook side, `stripe_webhook_refunds`'s `_resolve_refunds` (the `Refund.list` fallback for
API versions where `charge.refunded` doesn't embed the refunds list) is called **before** any
`Payment.select_for_update()` — the call site's own comment states why: holding the lock
across that outbound call "would block concurrent user-initiated cancels
(`cancellation_service` locks the same Payment rows) for its duration." The advisory balance
preview (`build_event_refund_preview`, which calls `stripe.Balance.retrieve`) takes no row
locks at all — it is read-only and racy by design.

**Mitigation:** `STRIPE_HTTP_TIMEOUT_SECONDS` (`revel/settings/stripe.py`, default 15,
configured on `stripe.default_http_client` in `stripe_service`/`stripe_webhooks` and every
other module that pins `stripe.api_key` at import) bounds stripe-python's own HTTP timeout,
which otherwise defaults to ~80s. `ATOMIC_REQUESTS` (see above) keeps the request's DB
transaction — and with it the row lock and the PgBouncer connection it's pinned to for the
transaction's lifetime (see the transaction-pooling note above) — open for as long as the
slowest outbound call inside it takes; bounding that call to 15s bounds the whole window to
15s instead of stripe-python's ~80s default.

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
   same after pulling.
