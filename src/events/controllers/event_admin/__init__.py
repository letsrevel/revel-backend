"""Event admin controllers package.

This package splits the event admin endpoints into logical groupings
while preserving the original endpoint order.
"""

from .core import EventAdminCoreController
from .invitation_requests import EventAdminInvitationRequestsController
from .invitations import EventAdminInvitationsController
from .rsvps import EventAdminRSVPsController
from .seating import EventAdminSeatingController
from .tickets import EventAdminTicketsController
from .tokens import EventAdminTokensController
from .waitlist import EventAdminWaitlistController
from .waitlist_offers import EventAdminWaitlistOffersController

# Controllers in order to preserve original endpoint ordering
EVENT_ADMIN_CONTROLLERS: list[type] = [
    EventAdminTokensController,
    EventAdminInvitationRequestsController,
    EventAdminCoreController,
    EventAdminTicketsController,
    EventAdminInvitationsController,
    EventAdminRSVPsController,
    EventAdminWaitlistController,
    EventAdminWaitlistOffersController,
    EventAdminSeatingController,
]

__all__ = [
    "EventAdminTokensController",
    "EventAdminInvitationRequestsController",
    "EventAdminCoreController",
    "EventAdminTicketsController",
    "EventAdminInvitationsController",
    "EventAdminRSVPsController",
    "EventAdminWaitlistController",
    "EventAdminWaitlistOffersController",
    "EventAdminSeatingController",
    "EVENT_ADMIN_CONTROLLERS",
]
