# ADR-0021: Post-deploy tasks with a run record

## Status

Proposed (2026-11-22). Issue #1508. This ADR covers a proof of concept (PoC) only. The PoC changes no
production behaviour: the runner is behind `FEATURE_POST_DEPLOY_TASKS`, which is off by default.

## Context

Some releases need a one-time action after the deploy that a schema migration cannot do. Today the
operator reads the release notes and runs a command by hand.

- v3.0.0: `backfill_unconfirmed_guest --before <cutoff>`.
- #1505: `provision_stripe_webhooks --sync`. Stripe sends only the event types that the endpoint
  subscribes to, so a skipped step means no alert when an automatic refund fails.

A self-hoster who follows `latest` and does not read the notes skips the step without knowing it.

Deploy facts that limit the design:

- `entrypoint.sh` runs `migrate` only for gunicorn.
- `infra/deploy-rollout.sh` runs `docker rollout` for `web`, then for `celery_default`. It
  force-recreates `beat` and `telegram`. Old and new containers run side by side during the rollout.
- Production uses PgBouncer in transaction pooling mode.

## Decision

Declare each task in code. Record each run in a table. Run pending tasks with one management command
after the web rollout.

### 1. Model

- Registry: `common/post_deploy.py` holds `PostDeployTask(id, release, description, run, skip_reason)`.
  Apps register their tasks from `AppConfig.ready`, because `common` never imports apps.
- Record: the table `common.PostDeployTaskRun` has one row per task. Fields: `task_id` (unique),
  `release`, `status` (`SUCCEEDED`, `FAILED`, `SKIPPED`), `attempts`, `started_at`, `finished_at` and
  `output`, a summary capped at 4000 characters.
- Order: tasks run by release (as a version), then by `id`.
- The record holds the last attempt only. # ponytail: the upgrade is a child table with one row per attempt.

### 2. When it runs

Recommended: a one-shot compose service `post_deploy`.

- It uses the same image, with `command: python manage.py run_post_deploy` and `restart: "no"`.
- `deploy-rollout.sh` runs it after `docker rollout web` and `docker rollout celery_default` finish.
- For a plain `docker compose pull && up -d` with `latest`, the same service runs on `up`. It must run
  after `migrate`. Use `depends_on` with the condition that `web` is healthy. Alternatively, the command
  runs `migrate` first. `migrate` is idempotent.
- The wiring is a follow-up. The PoC has the command only.

### 3. Ordering

The runner cannot reliably detect that all old containers have stopped. A task that needs this, such as
the v3.0.0 cutoff backfill, stays manual. The release notes carry a warning. A task flag such as
`after_rollout` is out of scope for the PoC.

### 4. Idempotency and safety

- Every task must be safe to run again.
- `run_post_deploy --dry-run` lists what would run and writes nothing.
- The runner locks the task's record row: `select_for_update(skip_locked=True)` inside a transaction.
  A second runner reports "busy" and moves on.
- The lock is not a session advisory lock. Session-level advisory locks are unsafe behind PgBouncer
  transaction pooling. The row lock belongs to the transaction, and Postgres releases it if the
  process dies.
- The task body runs in a savepoint. Its database writes roll back on failure. The `FAILED` record stays.
- The PoC has no timeout of its own. Tasks rely on client timeouts, for example the Stripe client.
  # ponytail: the upgrade is `statement_timeout` or a subprocess.

### 5. External side effects and failure

- On failure the runner records `FAILED`, logs the event `post_deploy_task_failed` (`task_id`,
  `release`, error class), and continues with the next task.
- The runner exits 0, so a failed task does not block the deploy. The next run retries the task.
- The log event is the hook for a Loki ruler alert, like `RefundFailed`.
- `SKIPPED` means a precondition is missing: Stripe is not configured, `BASE_URL` is not https, or no
  webhook secrets exist. It is not final. Every run checks the precondition again.
- The first task, `stripe-webhook-sync-1505`, runs the full `provision_stripe_webhooks --sync`. The
  sync removes stale event types. An event type that an operator added by hand to the endpoint is
  removed. The canonical lists in the command are the source of truth.

### 6. Visibility

PoC: `manage.py post_deploy_status [--check]`. It prints each task with its status. With `--check`, it
exits 1 when a task is `FAILED` or pending. Follow-ups:

- A read-only admin page, with an entry in the curated Unfold sidebar.
- A field in `/api/version` for pending or failed tasks.
- The Loki alert on `post_deploy_task_failed`.

### 7. Self-hosters

- `FEATURE_POST_DEPLOY_TASKS` is `False` in the PoC. A follow-up flips the default after the infra
  wiring ships. The flag is also the opt-out.
- The release notes still list each task and its manual command.
- The record is per task, not per release. After a jump over several versions, the runner runs every
  pending task, in release order and then by `id`.

### 8. What moves and what stays manual

Moves:

- The webhook `--sync` (first task).
- Future backfills that need no operator input.
- Cache warm-ups and search reindex.

Stays manual:

- `backfill_unconfirmed_guest --before <cutoff>`. The operator chooses the cutoff, and the rollout must
  be finished.
- Anything destructive, and anything that needs judgment.

## Options considered and rejected

Model:

- Django `RunPython` data migrations. They run in the migrate transaction, so an external call holds
  that transaction, and a failure rolls back the schema. Old code still runs during a rolling migrate.
  They use historical models only, and are hard to squash unless elidable.
- Rails `after_party`. The pattern is right: a named task, a record, a runner after migrate. It is a
  Rails tool, and it has no lock. A failed run records nothing.
- `django-post-deploy`. The fit is close: a decorator in `<app>/post_deploy.py`, a deploy command and
  run states. Its license is CC-BY-4.0, which is unusual for code. It documents no lock. It adds a
  dependency for a small amount of code.
- `django-after-deploy`. Last release in 2020. Not maintained.
- Laravel one-time-operations. It has a record table and an `--isolated` lock. The lock is a cache
  lock, and the package is for Laravel.
- Helm post-upgrade hook Job. It runs on every upgrade and keeps no record. Each task must carry its
  own idempotency.

Trigger:

- Web entrypoint. The step would run in every web replica while old code still serves. External calls
  would block the gunicorn start.
- Celery task on worker startup. Several workers start together, and the task runs on every restart.
  A failure is hidden inside the worker log.
- Manual `manage.py run_post_deploy` only. It keeps the manual burden. It stays as the fallback and
  for self-hosters who opt out.

Lock: a session advisory lock. Rejected for PgBouncer, see section 4.

## Consequences

- A release can register a task instead of an upgrade note. The record shows what ran on each instance.
- A failed task does not stop a deploy. Operators must watch the alert and the status command.
- The webhook sync removes hand-added event types on every instance that has the task pending.
- Tasks that need a finished rollout, or an operator value, stay manual.
- The runner adds one table and two commands. A rolling deploy stays safe, because the new columns have
  database defaults.

## Follow-ups

1. Infra: add the `post_deploy` compose service and the `deploy-rollout.sh` call. Turn the flag on in production.
2. Loki ruler alert on `post_deploy_task_failed`.
3. Admin read-only page and the `/api/version` field.
4. Self-hoster docs, and the release skill: prefer a registered task over an upgrade note.
5. Move future one-time steps. Keep cutoff-type backfills manual.
