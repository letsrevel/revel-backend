"""Thread-safe signal suppression for event-related side effects.

Uses `contextvars.ContextVar` instead of `post_save.disconnect()` to ensure
suppression is scoped to the current execution context (thread/coroutine),
not applied globally.
"""

import typing as t
from contextlib import contextmanager
from contextvars import ContextVar

__all__ = ["suppress_default_tier_creation", "suppress_event_notifications"]

_suppress_default_tier_creation: ContextVar[bool] = ContextVar("_suppress_default_tier_creation", default=False)

_suppress_event_notifications: ContextVar[bool] = ContextVar("_suppress_event_notifications", default=False)


@contextmanager
def suppress_default_tier_creation() -> t.Iterator[None]:
    """Suppress auto-creation of default ticket tier for events saved in this context.

    Used by `duplicate_event()` which copies tiers explicitly from the template.
    """
    token = _suppress_default_tier_creation.set(True)
    try:
        yield
    finally:
        _suppress_default_tier_creation.reset(token)


@contextmanager
def suppress_event_notifications() -> t.Iterator[None]:
    """Suppress EVENT_OPEN and follower notifications for events saved in this context.

    Used during batch materialization of recurring events to avoid notification spam.
    A single digest notification should be sent after the batch completes.
    """
    token = _suppress_event_notifications.set(True)
    try:
        yield
    finally:
        _suppress_event_notifications.reset(token)
