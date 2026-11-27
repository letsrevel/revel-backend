"""Signal receivers of the events app, one submodule for each group of senders.

All receivers of one sender are in one submodule, in the order they had before the split, so the order in which
Django calls them does not change. ``EventsConfig.ready`` imports this package, which registers every receiver.
Patch a name in the submodule that looks it up (for example ``events.signals.attendance.notification_requested``).
"""

from events.signals import attendance, event, membership, organization, user
from events.signals.organization import allow_cors_for_tenant_origin
from events.signals.user import stash_previous_link_state

__all__ = [
    "allow_cors_for_tenant_origin",
    "attendance",
    "event",
    "membership",
    "organization",
    "stash_previous_link_state",
    "user",
]
