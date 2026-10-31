"""Wallet passes (Apple and Google rails) must not print an address the holder may not see."""

import typing as t

import pytest

from events.models import Event, HeldSeriesPass, Ticket
from events.utils.visibility_settings import ResourceVisibility
from wallet.resolution import resolve_series_window, resolve_ticket_location

pytestmark = pytest.mark.django_db

ADDRESS = "123 Test Street"


def _set_visibility(event: Event, visibility: ResourceVisibility, address: str = ADDRESS) -> None:
    event.address = address
    event.visibility_settings = {"address_visibility": visibility.value}
    event.save()


@pytest.mark.parametrize(
    ("visibility", "shown"),
    [(ResourceVisibility.MEMBERS_ONLY, False), (ResourceVisibility.PUBLIC, True)],
)
def test_ticket_pass_address_follows_holder_visibility(
    event: Event, member_user: t.Any, ticket: Ticket, visibility: ResourceVisibility, shown: bool
) -> None:
    """A non-member ticket holder gets no address on a members-only event; public shows it."""
    _set_visibility(event, visibility)

    location = resolve_ticket_location(Ticket.objects.full().get(pk=ticket.pk))

    assert location.address == (ADDRESS if shown else None)


def test_series_pass_address_follows_holder_visibility(held_pass: HeldSeriesPass, covered_events: list[Event]) -> None:
    """The series pass borrows the representative event's address only if its holder may see it."""
    _set_visibility(covered_events[0], ResourceVisibility.MEMBERS_ONLY)
    assert resolve_series_window(held_pass).address is None

    _set_visibility(covered_events[0], ResourceVisibility.PUBLIC)
    assert resolve_series_window(HeldSeriesPass.objects.get(pk=held_pass.pk)).address == ADDRESS
