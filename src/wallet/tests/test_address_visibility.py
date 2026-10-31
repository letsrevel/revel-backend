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


def test_google_ticket_class_id_splits_by_address_visibility(
    google_wallet_configured_settings: None,
    event: Event,
    member: t.Any,
    member_user: t.Any,
    nonmember_user: t.Any,
    ticket: Ticket,
) -> None:
    """Google reuses an existing class ID as-is, so hidden-address holders need their own class."""
    from wallet.google.builder import build_ticket_payload

    _set_visibility(event, ResourceVisibility.MEMBERS_ONLY)
    hidden_ticket = Ticket.objects.create(
        event=event, user=nonmember_user, tier=ticket.tier, status=Ticket.TicketStatus.ACTIVE
    )

    visible = build_ticket_payload(Ticket.objects.full().get(pk=ticket.pk))["eventTicketClasses"][0]
    hidden = build_ticket_payload(Ticket.objects.full().get(pk=hidden_ticket.pk))["eventTicketClasses"][0]

    assert visible["id"] == f"3388000000012345678.test.event.{event.id}"
    assert visible["venue"]["address"]["defaultValue"]["value"] == ADDRESS
    assert hidden["id"] == f"3388000000012345678.test.event.{event.id}-noaddr"
    assert ADDRESS not in str(hidden)


def test_google_series_class_id_splits_by_address_visibility(
    google_wallet_configured_settings: None,
    held_pass: HeldSeriesPass,
    covered_events: list[Event],
    member: t.Any,
    nonmember_user: t.Any,
) -> None:
    from decimal import Decimal

    from wallet.google.builder import build_series_pass_payload

    _set_visibility(covered_events[0], ResourceVisibility.MEMBERS_ONLY)
    hidden_pass = HeldSeriesPass.objects.create(
        series_pass=held_pass.series_pass,
        user=nonmember_user,
        status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
        price_paid=Decimal("50.00"),
    )

    visible = build_series_pass_payload(HeldSeriesPass.objects.get(pk=held_pass.pk))["eventTicketClasses"][0]
    hidden = build_series_pass_payload(HeldSeriesPass.objects.get(pk=hidden_pass.pk))["eventTicketClasses"][0]

    base = f"3388000000012345678.test.series.{held_pass.series_pass.id}"
    assert visible["id"] == base
    assert visible["venue"]["address"]["defaultValue"]["value"] == ADDRESS
    assert hidden["id"] == f"{base}-noaddr"
    assert ADDRESS not in str(hidden)
