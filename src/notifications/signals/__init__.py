"""Notification signal handlers.

This package contains all signal handlers for triggering notifications.
Signal handlers are organized by domain to match the NotificationType enum structure.
"""

from django.dispatch import Signal

# Signal for requesting notification dispatch
# Expected kwargs:
#   - notification_type: NotificationType enum value
#   - user: RevelUser instance
#   - context: dict matching the notification type's context schema
notification_requested = Signal()

# Import all signal modules to ensure they're registered
# These imports MUST come after the signal definition
from notifications.signals import (  # noqa: E402, F401
    event,
    invitation,
    membership,
    payment,
    potluck,
    questionnaire,
    rsvp,
    telegram,
    ticket,
    user,
    waitlist,
)

__all__ = [
    "notification_requested",
    "event",
    "invitation",
    "membership",
    "payment",
    "potluck",
    "questionnaire",
    "rsvp",
    "telegram",
    "ticket",
    "user",
    "waitlist",
]
