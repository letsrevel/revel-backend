"""Thread-safe signal suppression for event-related side effects.

Uses `contextvars.ContextVar` instead of `post_save.disconnect()` to ensure
suppression is scoped to the current execution context (thread/coroutine),
not applied globally.
"""

import typing as t
from contextlib import contextmanager
from contextvars import ContextVar

__all__ = [
    "is_default_tier_creation_suppressed",
    "is_event_notifications_suppressed",
    "is_membership_granted_suppressed",
    "suppress_default_tier_creation",
    "suppress_event_notifications",
    "suppress_membership_granted",
]

_suppress_default_tier_creation: ContextVar[bool] = ContextVar("_suppress_default_tier_creation", default=False)

_suppress_event_notifications: ContextVar[bool] = ContextVar("_suppress_event_notifications", default=False)

_suppress_membership_granted: ContextVar[bool] = ContextVar("_suppress_membership_granted", default=False)


def is_default_tier_creation_suppressed() -> bool:
    """Return whether default ticket-tier creation is suppressed in this context."""
    return _suppress_default_tier_creation.get()


def is_event_notifications_suppressed() -> bool:
    """Return whether event notifications are suppressed in this context."""
    return _suppress_event_notifications.get()


def is_membership_granted_suppressed() -> bool:
    """Return whether MEMBERSHIP_GRANTED is suppressed for access rows saved in this context."""
    return _suppress_membership_granted.get()


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


@contextmanager
def suppress_membership_granted() -> t.Iterator[None]:
    """Suppress MEMBERSHIP_GRANTED (and MEMBERSHIP_CARD_UPDATED) for access rows written in this context.

    Used by the verified-email auto-link, which sends MEMBER_RECORD_LINKED instead (one message per organization).
    """
    token = _suppress_membership_granted.set(True)
    try:
        yield
    finally:
        _suppress_membership_granted.reset(token)
