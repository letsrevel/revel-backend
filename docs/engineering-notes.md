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
  real call-site injection proving this.
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
