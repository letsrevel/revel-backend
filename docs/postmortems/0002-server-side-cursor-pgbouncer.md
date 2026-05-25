# PM-0002: Server-Side Cursors Orphaned Under PgBouncer Transaction Pooling

## Summary

The daily Celery task `events.expire_subscriptions_past_grace` failed in
production on two consecutive nights with
`psycopg.errors.InvalidCursorName: cursor "_django_curs_..." does not exist`.
The task iterated a queryset with `QuerySet.iterator()` (a PostgreSQL
server-side cursor) while committing a per-row `transaction.atomic()` inside the
loop. Under PgBouncer transaction pooling, each commit recycles the backend
connection and orphans the cursor, so the next fetch crashes. A second task with
the identical pattern, `polls.tasks.close_polls_due`, was latent and would have
failed the same way once it had a due poll to process.

## Detection

Surfaced by an ad-hoc sweep of failed Celery tasks (`failed-tasks.sh prod`),
which showed two `FAILURE` rows for the same task at exactly `04:00:00 UTC`
(its `celery-beat` slot) on 2026-05-24 and 2026-05-25.

## Timeline

- **2026-05-11** — `expire_subscriptions_past_grace` shipped (#393, subscriptions Phase 1).
- **2026-05-24 04:00 UTC** — first observed failure.
- **2026-05-25 04:00 UTC** — second failure; investigated.
- **2026-05-25** — root cause identified, latent twin (`close_polls_due`, #446) found, fix opened (#458).

## Root Cause

`QuerySet.iterator()` opens a server-side cursor. The task ran it in autocommit
(no wrapping transaction) and then opened and committed a per-row
`transaction.atomic()` inside the loop. Production runs PgBouncer in
`POOL_MODE: transaction` (`DB_USE_PGBOUNCER=True`), which returns the backend
connection to the pool at every `COMMIT`. The named cursor lived on a
now-recycled backend, so the subsequent `fetchmany()` / `cursor.close()` routed
to a different backend and raised `InvalidCursorName`.

It evaded dev/CI because there is no pooler there, and a psycopg3 server-side
cursor opened in autocommit is `WITH HOLD`, so the mid-loop commit does not
invalidate it on a single backend. It was data-dependent in production too: the
crash only occurs once the loop body runs (≥1 row to process), which is why it
looked transient.

## Impact

The subscription-lifecycle task did not complete on affected nights, so the
`ACTIVE → PAST_DUE/EXPIRED` and `PAST_DUE → EXPIRED` transitions were cut short.
Net effect: some lapsed members could retain access past their paid period until
a subsequent successful run. No data was corrupted; rows committed before each
crash were durable. `close_polls_due` had not yet failed (no due polls under the
pooler).

## Resolution

1. **Settings (the fix):** `DISABLE_SERVER_SIDE_CURSORS = True` when
   `USE_PGBOUNCER` is on (`src/revel/settings/base.py`) — Django's documented
   requirement for transaction pooling, covering every `.iterator()` callsite.
2. **Source cleanup:** materialized the candidate IDs with `list(...)` instead of
   `.iterator()` in `expire_subscriptions_past_grace` and `close_polls_due`. The
   querysets are `values_list("id", flat=True)`, so streaming saved no real
   memory while the cursor introduced the fatal incompatibility.
3. **Guidance:** documented the anti-pattern in `revel-backend/CLAUDE.md`.

## Lessons Learned

- Server-side cursors and per-row commits are incompatible under transaction
  pooling; the safe pattern is "materialize IDs, then re-fetch each under its own
  lock."
- A class of bug that cannot reproduce locally (pooler-only) needs a written rule,
  not just a test. The fix is therefore primarily a settings guardrail.
- A failing periodic task should be alerted on, not discovered by an ad-hoc sweep.

## References

- Issue: #458
- Introduced by: #393 (subscriptions), #446 (polls — latent twin)
- Django docs: [Transaction pooling and server-side cursors](https://docs.djangoproject.com/en/5.2/ref/databases/#transaction-pooling-and-server-side-cursors)
