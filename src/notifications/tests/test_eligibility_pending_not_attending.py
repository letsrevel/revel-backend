"""PENDING ticket holders are not address-eligible in notifications (#1278)."""

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Organization, Ticket, TicketTier
from events.models.mixins import ResourceVisibility
from notifications.service.eligibility import BatchParticipationChecker

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "status,expected",
    [
        (Ticket.TicketStatus.ACTIVE, True),
        (Ticket.TicketStatus.CHECKED_IN, True),
        (Ticket.TicketStatus.PENDING, False),
        (Ticket.TicketStatus.CANCELLED, False),
    ],
)
def test_has_attending_ticket_by_status(
    organization: Organization, nonmember_user: RevelUser, status: Ticket.TicketStatus, expected: bool
) -> None:
    event = Event.objects.create(
        organization=organization,
        name="E",
        start=timezone.now(),
        visibility_settings={"address_visibility": ResourceVisibility.ATTENDEES_ONLY},
    )
    tier = TicketTier.objects.create(event=event, name="General")
    Ticket.objects.create(event=event, user=nonmember_user, tier=tier, guest_name="G", status=status)

    checker = BatchParticipationChecker(event)

    assert checker.has_attending_ticket(nonmember_user.id) is expected
