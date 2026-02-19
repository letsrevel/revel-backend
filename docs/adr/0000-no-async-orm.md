# ADR-0000: Synchronous ORM Over Django Async ORM

## Status

Accepted

## Context

Django has been progressively adding async support since version 3.1 (async views) and
4.1 (async ORM interface). This raises the question of whether Revel should adopt async
views and the async ORM to improve throughput.

After analyzing the codebase and deployment architecture, we identified three factors
that make the switch inadvisable.

### Most I/O is database-bound

The vast majority of request-response I/O in Revel is database queries. External service
calls are rare in the hot path:

- **Stripe** — checkout session creation and account operations (occasional)
- **Google OAuth** — token verification during SSO login (occasional)

All other external I/O (email, Telegram, Pushover, ClamAV, LLM evaluation, IP2Location,
thumbnail generation, GDPR exports) is already offloaded to **42 Celery tasks**. Async
views would not improve throughput for database-bound requests.

### Pervasive use of atomic transactions

The codebase relies heavily on transactional integrity:

| Pattern | Count |
|---|---|
| `@transaction.atomic` decorators | 67 |
| `transaction.atomic()` context managers | 20 |
| `select_for_update()` pessimistic locks | 16 |
| `F()` atomic field updates | 10+ |
| `transaction.on_commit()` callbacks | 34+ |

Django does **not** support `transaction.atomic()` in async contexts
([ticket #33882](https://code.djangoproject.com/ticket/33882) is still open). This means
every transactional service function would need to be wrapped in `sync_to_async`,
dispatching it to the thread pool executor. The result: async views that immediately
delegate to threads — the same concurrency model as the current Gunicorn `gthread`
setup, but with added indirection and overhead.

### Complexity cost with no clear payoff

The project is designed for contributors with varying experience levels. Async Python
introduces:

- The **colored function problem**: `async`/`await` propagates virally through the call
  chain, requiring every caller to become async
- **Harder debugging**: stack traces are less readable, `pdb` behaves differently, and
  race conditions in async code are subtler than in thread-based code
- **Three concurrency models to reason about**: the event loop, Gunicorn threads, and
  Celery workers — instead of the current two (threads + Celery)

### Deployment architecture already handles concurrency

The production deployment uses Gunicorn with threaded workers:

```
gunicorn revel.wsgi:application \
    --worker-class gthread \
    --workers 6 \
    --threads 4
```

This provides 24 concurrent request slots, which is appropriate for a database-bound
application behind PgBouncer connection pooling. Switching to ASGI (e.g., Uvicorn) would
not increase effective concurrency for database-bound workloads.

## Decision

**Stay on synchronous Django with the WSGI stack.** Do not adopt async views or the
async ORM.

The concurrency strategy remains:

- **Request concurrency**: Gunicorn `gthread` workers (process + thread model)
- **Background I/O**: Celery tasks for all external service calls, file processing, and
  long-running operations
- **Database concurrency**: `transaction.atomic`, `select_for_update`, and `F()`
  expressions for safe concurrent access

## Consequences

**Positive:**

- **Simple mental model**: one execution model in request handling (synchronous), one for
  background work (Celery)
- **Full ORM feature set**: `transaction.atomic`, `select_for_update`, signals, and
  `on_commit` callbacks work without wrappers or limitations
- **Lower barrier to contribution**: no async/await knowledge required; standard Django
  patterns apply
- **No migration risk**: avoids a large-scale refactor with marginal benefit

**Negative:**

- **No async fan-out in views**: if a view ever needs to call multiple external services
  concurrently, it must delegate to Celery or use `concurrent.futures` manually
- **ASGI ecosystem**: some newer Django libraries assume ASGI; we may need sync
  alternatives

**Neutral:**

- This decision should be **revisited** if Django merges async `transaction.atomic`
  support and the project's I/O profile shifts toward more external service calls in the
  request path
- The `uvicorn` and `uvloop` dependencies can be removed from `pyproject.toml` since
  they are unused
