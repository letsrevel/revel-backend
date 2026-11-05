"""Potluck claims are released only when the user is no longer entitled to hold them (#1278)."""

import typing as t

import pytest

from accounts.models import RevelUser
from events.models import Event, EventRSVP, Ticket, TicketTier
from events.models.potluck import PotluckItem

pytestmark = pytest.mark.django_db


def _ticket(event: Event, user: RevelUser, status: Ticket.TicketStatus) -> Ticket:
    tier = TicketTier.objects.get_or_create(event=event, name="General")[0]
    return Ticket.objects.create(event=event, user=user, tier=tier, guest_name="G", status=status)


def _claim(event: Event, user: RevelUser) -> PotluckItem:
    return PotluckItem.objects.create(event=event, name="Item", item_type="food", assignee=user, created_by=user)


def _set_status(ticket: Ticket, status: Ticket.TicketStatus) -> None:
    ticket.status = status
    ticket.save(update_fields=["status"])


def test_staff_keeps_claims_when_ticket_is_cancelled(
    event: Event, organization_staff_user: RevelUser, staff_member: t.Any
) -> None:
    ticket = _ticket(event, organization_staff_user, Ticket.TicketStatus.ACTIVE)
    item = _claim(event, organization_staff_user)

    _set_status(ticket, Ticket.TicketStatus.CANCELLED)

    item.refresh_from_db()
    assert item.assignee == organization_staff_user


def test_pending_to_cancelled_releases_claims(event: Event, nonmember_user: RevelUser) -> None:
    """Covers a claim made by a PENDING holder before attendance was tightened."""
    ticket = _ticket(event, nonmember_user, Ticket.TicketStatus.PENDING)
    item = _claim(event, nonmember_user)

    _set_status(ticket, Ticket.TicketStatus.CANCELLED)

    item.refresh_from_db()
    assert item.assignee is None


def test_deleting_one_of_two_active_tickets_keeps_claims(event: Event, nonmember_user: RevelUser) -> None:
    first = _ticket(event, nonmember_user, Ticket.TicketStatus.ACTIVE)
    _ticket(event, nonmember_user, Ticket.TicketStatus.ACTIVE)
    item = _claim(event, nonmember_user)

    first.delete()

    item.refresh_from_db()
    assert item.assignee == nonmember_user


def test_rsvp_maybe_keeps_claims_for_attending_ticket_holder(event: Event, nonmember_user: RevelUser) -> None:
    _ticket(event, nonmember_user, Ticket.TicketStatus.ACTIVE)
    rsvp = EventRSVP.objects.create(event=event, user=nonmember_user, status=EventRSVP.RsvpStatus.YES)
    item = _claim(event, nonmember_user)

    rsvp.status = EventRSVP.RsvpStatus.MAYBE
    rsvp.save()

    item.refresh_from_db()
    assert item.assignee == nonmember_user


def test_ticket_save_without_status_skips_transition_logic(event: Event, nonmember_user: RevelUser) -> None:
    """A save(update_fields=[...]) that excludes status never counts as a transition."""
    ticket = _ticket(event, nonmember_user, Ticket.TicketStatus.CANCELLED)
    item = _claim(event, nonmember_user)

    ticket.guest_name = "Renamed"
    ticket.save(update_fields=["guest_name"])

    item.refresh_from_db()
    assert item.assignee == nonmember_user
