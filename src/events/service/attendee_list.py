"""Attendee list for an event, limited to accounts known to the organization for privileged viewers."""

from django.db.models import OuterRef, QuerySet

from accounts.models import RevelUser
from events.models import Event
from events.service.holder_visibility import known_holder_q


def get_event_attendee_list(event: Event, viewer: RevelUser) -> QuerySet[RevelUser]:
    """Return the attendees ``viewer`` may see.

    Owners, staff and superusers see every attendee except accounts whose only link to the organization
    is staff-originated (box-office ticket, staff RSVP): their profile is not theirs to show. Other
    viewers see the opt-in based list from ``Event.attendees`` unchanged.
    """
    attendees = event.attendees(viewer)
    if viewer.is_superuser or viewer.is_staff or event.organization.is_owner_or_staff(viewer):
        attendees = attendees.filter(known_holder_q(event.organization_id, OuterRef("pk")))
    return attendees.distinct()
