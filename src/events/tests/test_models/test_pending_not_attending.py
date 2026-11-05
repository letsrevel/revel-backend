"""Only ACTIVE/CHECKED_IN tickets count as attendance; PENDING does not (#1278)."""

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Organization, Ticket, TicketTier
from events.models.mixins import ResourceVisibility

pytestmark = pytest.mark.django_db


def _ticket(event: Event, user: RevelUser, status: Ticket.TicketStatus) -> Ticket:
    tier = TicketTier.objects.get_or_create(event=event, name="General")[0]
    return Ticket.objects.create(event=event, user=user, tier=tier, guest_name="G", status=status)


@pytest.fixture
def attendee_only_event(organization: Organization) -> Event:
    return Event.objects.create(
        organization=organization,
        name="Attendees Only",
        start=timezone.now(),
        address="123 Test Street",
        visibility_settings={"address_visibility": ResourceVisibility.ATTENDEES_ONLY},
    )


@pytest.mark.parametrize(
    "status,expected",
    [
        (Ticket.TicketStatus.ACTIVE, True),
        (Ticket.TicketStatus.CHECKED_IN, True),
        (Ticket.TicketStatus.PENDING, False),
        (Ticket.TicketStatus.CANCELLED, False),
    ],
)
def test_attendee_only_info_by_ticket_status(
    attendee_only_event: Event, nonmember_user: RevelUser, status: Ticket.TicketStatus, expected: bool
) -> None:
    """Address and cancellation reason are visible only to ACTIVE and CHECKED_IN holders."""
    _ticket(attendee_only_event, nonmember_user, status)

    assert attendee_only_event.can_user_see_address(nonmember_user) is expected
    assert attendee_only_event.can_user_see_cancellation_reason(nonmember_user) is expected


def test_staff_attendees_view_includes_checked_in_but_not_pending(
    attendee_only_event: Event,
    organization_owner_user: RevelUser,
    nonmember_user: RevelUser,
    member_user: RevelUser,
) -> None:
    """The staff attendee list keeps checked-in attendees and omits PENDING holders."""
    _ticket(attendee_only_event, nonmember_user, Ticket.TicketStatus.CHECKED_IN)
    _ticket(attendee_only_event, member_user, Ticket.TicketStatus.PENDING)

    assert list(attendee_only_event.attendees(organization_owner_user)) == [nonmember_user]
