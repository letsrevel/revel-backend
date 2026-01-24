# src/events/admin/__init__.py
"""Events admin module - split for maintainability.

Django autodiscover will import this module, which triggers registration
of all admin classes via the @admin.register decorators in submodules.
"""

# Import all admin modules to trigger @admin.register decorators
from events.admin.announcement import AnnouncementAdmin
from events.admin.blacklist import BlacklistAdmin, WhitelistRequestAdmin
from events.admin.event import (
    EventAdmin,
    EventInvitationAdmin,
    EventInvitationRequestAdmin,
    EventResourceAdmin,
    EventRSVPAdmin,
    EventSeriesAdmin,
    EventTokenAdmin,
    EventWaitListAdmin,
    PendingEventInvitationAdmin,
    PotluckItemAdmin,
)
from events.admin.organization import (
    MembershipTierAdmin,
    OrganizationAdmin,
    OrganizationMemberAdmin,
    OrganizationMembershipRequestAdmin,
    OrganizationQuestionnaireAdmin,
    OrganizationStaffAdmin,
    OrganizationTokenAdmin,
)
from events.admin.preferences import (
    AttendeeVisibilityFlagAdmin,
    GeneralUserPreferencesAdmin,
)
from events.admin.ticket import (
    PaymentAdmin,
    TicketAdmin,
    TicketTierAdmin,
)
from events.admin.venue import (
    VenueAdmin,
    VenueSeatAdmin,
    VenueSectorAdmin,
)

__all__ = [
    # Announcement
    "AnnouncementAdmin",
    # Organization
    "OrganizationAdmin",
    "OrganizationQuestionnaireAdmin",
    "MembershipTierAdmin",
    "OrganizationStaffAdmin",
    "OrganizationMemberAdmin",
    "OrganizationMembershipRequestAdmin",
    "OrganizationTokenAdmin",
    # Event
    "EventAdmin",
    "EventSeriesAdmin",
    "EventInvitationAdmin",
    "EventRSVPAdmin",
    "PendingEventInvitationAdmin",
    "EventTokenAdmin",
    "EventInvitationRequestAdmin",
    "EventWaitListAdmin",
    "PotluckItemAdmin",
    "EventResourceAdmin",
    # Venue
    "VenueAdmin",
    "VenueSectorAdmin",
    "VenueSeatAdmin",
    # Ticket
    "TicketTierAdmin",
    "TicketAdmin",
    "PaymentAdmin",
    # Preferences
    "GeneralUserPreferencesAdmin",
    "AttendeeVisibilityFlagAdmin",
    # Blacklist
    "BlacklistAdmin",
    "WhitelistRequestAdmin",
]
