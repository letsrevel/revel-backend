# ADR-0007: Let Celery Exceptions Propagate

## Status

Accepted

## Context

A common pattern in Celery-based projects is to wrap entire task bodies in
`try-except` blocks to prevent task failures:

```python
# Anti-pattern
@app.task
def send_notification(user_id: int) -> None:
    try:
        user = User.objects.get(id=user_id)
        send_email(user)
    except Exception:
        logger.exception("Failed to send notification")
        # Task "succeeds" silently
```

While this prevents tasks from appearing as failures in monitoring, it has serious
drawbacks:

- **Bugs are hidden**: Errors are logged but never surfaced to alerting systems
- **Retry mechanism is broken**: Celery's built-in retry logic only triggers on task
  failure. Swallowed exceptions bypass it entirely
- **Silent data corruption**: A task that "succeeds" despite an error may leave the
  system in an inconsistent state
- **Debugging is harder**: Issues only show up as log lines, not as failed tasks in
  Flower or Celery's result backend

## Decision

**Do not catch exceptions for the sake of catching them in Celery tasks.** Let
exceptions propagate naturally so tasks fail visibly.

```python
# Correct approach
@app.task(bind=True, max_retries=3)
def send_notification(self, user_id: int) -> None:
    user = User.objects.get(id=user_id)  # DoesNotExist propagates
    send_email(user)                      # SMTP errors propagate
```

When retries are appropriate, use Celery's built-in retry mechanism:

```python
@app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_notification(self, user_id: int) -> None:
    try:
        send_email(User.objects.get(id=user_id))
    except SMTPError as exc:
        raise self.retry(exc=exc)  # Celery handles retry scheduling
```

!!! note "Targeted Exception Handling Is Fine"

    This decision is about **blanket** `try-except` blocks. Catching **specific**
    exceptions for retry logic or to make a deliberate business decision is perfectly
    acceptable.

## Consequences

**Positive:**

- Bugs are **visible**: failed tasks appear in monitoring and alerting
- Celery's **retry mechanism works correctly**: only triggers on actual failures
- The **observability stack** (Flower, OpenTelemetry) captures errors
  automatically
- **Debugging is straightforward**: stack traces are preserved in task results

**Negative:**

- Failed tasks may require **manual intervention** (inspection, replay)
- Task queues may accumulate failed tasks if not monitored

**Neutral:**

- Critical tasks should use Celery's built-in retry mechanism with appropriate
  `max_retries` and `default_retry_delay` settings
- Dead-letter queues or failure callbacks can handle permanently failed tasks
