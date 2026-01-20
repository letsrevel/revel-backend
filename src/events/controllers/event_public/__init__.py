from .attendance import EventPublicAttendanceController
from .details import EventPublicDetailsController
from .discovery import EventPublicDiscoveryController
from .guest import EventPublicGuestController
from .tickets import EventPublicTicketsController

# Controllers in order to preserve path resolution.
# Non-event_id routes (discovery) MUST come first to avoid being matched
# by the /{uuid:event_id} catch-all pattern in other controllers.
EVENT_PUBLIC_CONTROLLERS: list[type] = [
    EventPublicDiscoveryController,  # Non-event_id routes: /, /calendar, /tokens, /checkout, etc.
    EventPublicDetailsController,  # /{org_slug}/event/... and /{uuid:event_id}
    EventPublicAttendanceController,  # /{uuid:event_id}/rsvp, waitlist, questionnaire, etc.
    EventPublicTicketsController,  # /{uuid:event_id}/tickets/...
    EventPublicGuestController,  # /{uuid:event_id}/.../public
]

__all__ = [
    "EventPublicDiscoveryController",
    "EventPublicDetailsController",
    "EventPublicAttendanceController",
    "EventPublicTicketsController",
    "EventPublicGuestController",
    "EVENT_PUBLIC_CONTROLLERS",
]
