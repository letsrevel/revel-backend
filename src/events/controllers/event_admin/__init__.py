"""Event admin controllers package.

This package splits the event admin endpoints into logical groupings
while preserving the original endpoint order.
"""

from .core import EventAdminCoreController
from .invitation_requests import EventAdminInvitationRequestsController
from .invitations import EventAdminInvitationsController
from .rsvps import EventAdminRSVPsController
from .tickets import EventAdminTicketsController
from .tokens import EventAdminTokensController
from .waitlist import EventAdminWaitlistController

# Controllers in order to preserve original endpoint ordering
EVENT_ADMIN_CONTROLLERS: list[type] = [
    EventAdminTokensController,
    EventAdminInvitationRequestsController,
    EventAdminCoreController,
    EventAdminTicketsController,
    EventAdminInvitationsController,
    EventAdminRSVPsController,
    EventAdminWaitlistController,
]

__all__ = [
    "EventAdminTokensController",
    "EventAdminInvitationRequestsController",
    "EventAdminCoreController",
    "EventAdminTicketsController",
    "EventAdminInvitationsController",
    "EventAdminRSVPsController",
    "EventAdminWaitlistController",
    "EVENT_ADMIN_CONTROLLERS",
]
