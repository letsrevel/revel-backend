"""``has_relationship(..., include_attendees=True)``: event-history standing used by staff event routes."""

from decimal import Decimal

import pytest

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.exceptions import InvitationRequiredError
from events.models import (
    Event,
    EventRSVP,
    EventSeries,
    HeldSeriesPass,
    Organization,
    SeriesPass,
    Ticket,
    TicketTier,
)
from events.service import organization_service

pytestmark = pytest.mark.django_db


def _ticket(event: Event, user: RevelUser, status: Ticket.TicketStatus) -> Ticket:
    tier = TicketTier.objects.create(event=event, name="T", price=Decimal("0"))
    return Ticket.objects.create(event=event, user=user, tier=tier, status=status, guest_name="X")


def test_stranger_has_no_relationship_either_way(organization: Organization, nonmember_user: RevelUser) -> None:
    assert not organization_service.has_relationship(organization, nonmember_user)
    assert not organization_service.has_relationship(organization, nonmember_user, include_attendees=True)
    with pytest.raises(InvitationRequiredError):
        organization_service.require_relationship(organization, nonmember_user, include_attendees=True)


@pytest.mark.parametrize("status", list(Ticket.TicketStatus))
def test_ticket_in_any_status_counts_only_with_attendees(
    organization: Organization, event: Event, nonmember_user: RevelUser, status: Ticket.TicketStatus
) -> None:
    _ticket(event, nonmember_user, status)
    assert not organization_service.has_relationship(organization, nonmember_user)
    assert organization_service.has_relationship(organization, nonmember_user, include_attendees=True)


@pytest.mark.parametrize("status", list(EventRSVP.RsvpStatus))
def test_rsvp_in_any_status_counts_only_with_attendees(
    organization: Organization, event: Event, nonmember_user: RevelUser, status: EventRSVP.RsvpStatus
) -> None:
    EventRSVP.objects.create(event=event, user=nonmember_user, status=status)
    assert not organization_service.has_relationship(organization, nonmember_user)
    assert organization_service.has_relationship(organization, nonmember_user, include_attendees=True)


def test_held_series_pass_counts_only_with_attendees(
    organization: Organization, event_series: EventSeries, nonmember_user: RevelUser
) -> None:
    series_pass = SeriesPass.objects.create(
        event_series=event_series,
        name="Season",
        price=Decimal("10.00"),
        pro_rata_discount=Decimal("0.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
    )
    HeldSeriesPass.objects.create(series_pass=series_pass, user=nonmember_user, price_paid=Decimal("10.00"))
    assert not organization_service.has_relationship(organization, nonmember_user)
    assert organization_service.has_relationship(organization, nonmember_user, include_attendees=True)


def test_attendance_at_another_organization_does_not_count(
    organization: Organization, event: Event, nonmember_user: RevelUser, revel_user_factory: RevelUserFactory
) -> None:
    other_org = Organization.objects.create(name="Other", slug="other-org", owner=revel_user_factory())
    other_event = Event.objects.create(organization=other_org, name="Elsewhere", start=event.start)
    _ticket(other_event, nonmember_user, Ticket.TicketStatus.ACTIVE)
    EventRSVP.objects.create(event=other_event, user=nonmember_user, status=EventRSVP.RsvpStatus.YES)
    assert not organization_service.has_relationship(organization, nonmember_user, include_attendees=True)
