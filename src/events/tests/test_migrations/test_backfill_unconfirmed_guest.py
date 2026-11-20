"""Tests for the 0152 backfill of ``Ticket.unconfirmed_guest`` (#1430).

Exercised directly against the live app registry, like the other backfill tests here.
"""

import importlib
import typing as t
from decimal import Decimal

import pytest
from django.apps import apps as django_apps

from accounts.models import RevelUser
from events.models import Event, Payment, Ticket, TicketSaleSource, TicketTier

pytestmark = pytest.mark.django_db

backfill = importlib.import_module("events.migrations.0152_backfill_unconfirmed_guest").backfill


def _ticket(
    event: Event,
    user: RevelUser,
    method: str,
    source: str | None = TicketSaleSource.CHECKOUT,
    status: str = Ticket.TicketStatus.PENDING,
) -> Ticket:
    tier = TicketTier.objects.create(
        event=event, name=f"T{Ticket.objects.count()}", price=Decimal("10"), payment_method=method
    )
    ticket = Ticket.objects.create(event=event, user=user, tier=tier, status=status, guest_name="X")
    Ticket.objects.filter(pk=ticket.pk).update(sale_source=source)
    return ticket


def _flag(ticket: Ticket) -> bool:
    ticket.refresh_from_db()
    return ticket.unconfirmed_guest


def test_backfill_flags_only_paid_guest_checkouts(
    event: Event, nonmember_user: RevelUser, member_user: RevelUser, payment_factory: t.Callable[..., Payment]
) -> None:
    RevelUser.objects.filter(pk=nonmember_user.pk).update(guest=True)
    nonmember_user.refresh_from_db()
    online = TicketTier.PaymentMethod.ONLINE
    online_ticket = _ticket(event, nonmember_user, online)
    legacy_source = _ticket(event, nonmember_user, online, source=None)
    active_online = _ticket(event, nonmember_user, online, status=Ticket.TicketStatus.ACTIVE)
    with_payment = _ticket(event, nonmember_user, TicketTier.PaymentMethod.OFFLINE)
    payment_factory(with_payment)
    free_guest = _ticket(event, nonmember_user, TicketTier.PaymentMethod.FREE)
    offline_guest = _ticket(event, nonmember_user, TicketTier.PaymentMethod.OFFLINE)
    box_office = _ticket(event, nonmember_user, online, source=TicketSaleSource.BOX_OFFICE_SALE)
    registered = _ticket(event, member_user, online)

    backfill(django_apps, None)

    assert all(_flag(tk) for tk in (online_ticket, legacy_source, active_online, with_payment))
    assert not any(_flag(tk) for tk in (free_guest, offline_guest, box_office, registered))

    backfill(django_apps, None)  # idempotent
    assert _flag(online_ticket)
    assert not _flag(registered)
