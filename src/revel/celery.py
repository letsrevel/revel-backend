"""Celery setup for Revel."""

import os
import typing as t

import structlog
from celery import Celery
from celery.signals import task_postrun, task_prerun
from opentelemetry import trace

# Set the default Django settings module for the 'celery' program.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "revel.settings")

app = Celery("revel")

# Using a string here means the worker doesn't have to serialize
# the configuration object to child processes.
# - namespace='CELERY' means all celery-related configuration keys
#   should have a `CELERY_` prefix.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Load task modules from all registered Django apps.
app.autodiscover_tasks()


# Observability: Celery task context enrichment


@task_prerun.connect
def celery_task_prerun(task_id: str, task: t.Any, *args: t.Any, **kwargs: t.Any) -> None:
    """Bind Celery task context to structlog before task execution.

    Args:
        task_id: Unique ID of the Celery task
        task: The Celery task instance
        args: Task positional arguments
        kwargs: Task keyword arguments
    """
    from django.conf import settings

    if not settings.ENABLE_OBSERVABILITY:
        return

    # Clear any previous context
    structlog.contextvars.clear_contextvars()

    # Get current trace context (if tracing is active)
    trace_id = None
    span = trace.get_current_span()
    if span and span.get_span_context().is_valid:
        # Format trace_id as hex string (16 bytes -> 32 hex chars)
        trace_id = format(span.get_span_context().trace_id, "032x")

    # Bind task context
    context = {
        "task_id": task_id,
        "task_name": task.name,
        "queue": task.request.delivery_info.get("routing_key", "default")
        if hasattr(task.request, "delivery_info")
        else "default",
        "retries": task.request.retries if hasattr(task.request, "retries") else 0,
    }

    # Add trace_id if available (for log-to-trace correlation)
    if trace_id:
        context["trace_id"] = trace_id

    structlog.contextvars.bind_contextvars(**context)


@task_postrun.connect
def celery_task_postrun(*args: t.Any, **kwargs: t.Any) -> None:
    """Clear structlog context after task execution.

    Args:
        args: Signal arguments
        kwargs: Signal keyword arguments
    """
    from django.conf import settings

    if not settings.ENABLE_OBSERVABILITY:
        return

    # Clear context after task completes
    structlog.contextvars.clear_contextvars()


# run:
# celery -A revel worker -l INFO
# celery -A revel beat -l INFO --scheduler django_celery_beat.schedulers:DatabaseScheduler
