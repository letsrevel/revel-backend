"""Gap-free, per-organization fiscal ticket numbers (#1060, #1061, #1064)."""

import importlib
import threading
import typing as t
from decimal import Decimal

import pytest
from django.apps import apps as django_apps
from django.db import connection
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Organization, Ticket, TicketNumberSequence, TicketTier
from events.service.ticket_number_service import assign_ticket_numbers, format_ticket_number

pytestmark = pytest.mark.django_db

_backfill = importlib.import_module("events.migrations.0127_backfill_ticket_numbers").backfill


@pytest.fixture
def make_ticket(ticket_factory: t.Callable[..., Ticket], event_ticket_tier: TicketTier) -> t.Callable[..., Ticket]:
    """``ticket_factory`` pinned to one tier (it would otherwise create a tier per ticket)."""
    return lambda **kwargs: ticket_factory(tier=event_ticket_tier, **kwargs)


def _issue(make_ticket: t.Callable[..., Ticket], n: int) -> list[Ticket]:
    return [make_ticket() for _ in range(n)]


class TestAssignment:
    def test_issued_tickets_get_consecutive_numbers(
        self, organization: Organization, make_ticket: t.Callable[..., Ticket]
    ) -> None:
        tickets = _issue(make_ticket, 3)

        assert [tk.ticket_number for tk in tickets] == [1, 2, 3]
        assert {tk.ticket_series for tk in tickets} == {"ORG"}
        assert all(tk.issued_at is not None for tk in tickets)
        assert format_ticket_number(tickets[0]) == "ORG-000001"
        assert TicketNumberSequence.objects.get(organization=organization).last_number == 3

    def test_pending_tickets_wait_for_activation(self, make_ticket: t.Callable[..., Ticket]) -> None:
        pending = make_ticket(status=Ticket.TicketStatus.PENDING)
        assert pending.ticket_number is None
        assert format_ticket_number(pending) == ""

        issued = make_ticket()
        pending.status = Ticket.TicketStatus.ACTIVE
        pending.save(update_fields=["status"])

        assert issued.ticket_number == 1
        assert pending.ticket_number == 2  # numbered when issued, not when reserved

    def test_numbers_are_never_reassigned(self, make_ticket: t.Callable[..., Ticket]) -> None:
        ticket = make_ticket()
        ticket.status = Ticket.TicketStatus.CANCELLED
        ticket.save(update_fields=["status"])
        ticket.status = Ticket.TicketStatus.ACTIVE
        ticket.save(update_fields=["status"])

        ticket.refresh_from_db()
        assert ticket.ticket_number == 1
        assert make_ticket().ticket_number == 2

    def test_each_organization_has_its_own_series(
        self,
        make_ticket: t.Callable[..., Ticket],
        organization_owner_user: RevelUser,
        member_user: RevelUser,
    ) -> None:
        other_org = Organization.objects.create(name="Other Club", slug="other-club", owner=organization_owner_user)
        other_event = Event.objects.create(organization=other_org, name="Other", slug="other", start=timezone.now())
        other_tier = TicketTier.objects.create(event=other_event, name="Free")

        make_ticket()
        other = Ticket.objects.create(event=other_event, tier=other_tier, user=member_user)

        assert (other.ticket_series, other.ticket_number) == ("OTHERCLUB", 1)

    def test_series_survives_a_slug_rename(
        self, organization: Organization, make_ticket: t.Callable[..., Ticket]
    ) -> None:
        make_ticket()
        organization.slug = "renamed"
        organization.save(update_fields=["slug"])

        assert make_ticket().ticket_series == "ORG"

    def test_bulk_checkout_numbers_tickets(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        """bulk_create skips post_save: the checkout writer numbers free tickets itself."""
        from events.schema import TicketPurchaseItem
        from events.service.batch_ticket_service import BatchTicketService

        tier = TicketTier.objects.create(
            event=event, name="Free", price=Decimal("0"), payment_method=TicketTier.PaymentMethod.FREE
        )
        event.max_tickets_per_user = 5
        event.save(update_fields=["max_tickets_per_user"])

        tickets = BatchTicketService(event, tier, member_user).create_batch(
            [TicketPurchaseItem(guest_name="A"), TicketPurchaseItem(guest_name="B")]
        )

        assert isinstance(tickets, list)
        numbers = Ticket.objects.filter(pk__in=[tk.pk for tk in tickets]).values_list("ticket_number", flat=True)
        assert set(numbers) == {1, 2}

    def test_assign_is_idempotent(self, make_ticket: t.Callable[..., Ticket]) -> None:
        ticket = make_ticket()

        assign_ticket_numbers([ticket, ticket])
        assign_ticket_numbers(Ticket.objects.all())

        assert ticket.ticket_number == 1
        assert TicketNumberSequence.objects.get().last_number == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_issuance_is_gap_free(
    organization: Organization, event: Event, event_ticket_tier: TicketTier, member_user: RevelUser
) -> None:
    """Real concurrent transactions serialize on the sequence row: unique, gap-free numbers.

    ``transaction=True`` because the point is cross-connection locking, which the default
    rolled-back test transaction cannot exercise.
    """
    pending = [
        Ticket.objects.create(event=event, tier=event_ticket_tier, user=member_user, status=Ticket.TicketStatus.PENDING)
        for _ in range(8)
    ]
    errors: list[BaseException] = []

    def _activate(ticket: Ticket) -> None:
        try:
            ticket.status = Ticket.TicketStatus.ACTIVE
            ticket.save(update_fields=["status"])
        except BaseException as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=_activate, args=(ticket,)) for ticket in pending]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    numbers = list(Ticket.objects.filter(event=event).values_list("ticket_number", flat=True))
    assert len(numbers) == 8
    assert set(numbers) == set(range(1, 9))


def test_backfill_numbers_existing_tickets_in_creation_order(
    organization: Organization, make_ticket: t.Callable[..., Ticket]
) -> None:
    tickets = _issue(make_ticket, 3)
    cancelled = make_ticket(status=Ticket.TicketStatus.PENDING)
    cancelled.status = Ticket.TicketStatus.CANCELLED
    cancelled.save(update_fields=["status"])
    Ticket.objects.update(ticket_series="", ticket_number=None, issued_at=None)
    TicketNumberSequence.objects.all().delete()

    _backfill(django_apps, None)

    numbered = list(Ticket.objects.filter(pk__in=[tk.pk for tk in tickets]).order_by("created_at", "pk"))
    assert [tk.ticket_number for tk in numbered] == [1, 2, 3]
    assert all(tk.issued_at == tk.created_at for tk in numbered)
    cancelled.refresh_from_db()
    assert cancelled.ticket_number is None
    assert make_ticket().ticket_number == 4  # the live sequence continues the backfilled series
