"""Check-in locks the ticket and re-checks its status before it writes CHECKED_IN (#1482)."""

import re
import typing as t
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Ticket, TicketTier
from events.service import check_in_service

pytestmark = pytest.mark.django_db

_ensure_check_in_open = check_in_service._ensure_check_in_open
EXPECTED_CHECK_IN_QUERIES = 20


@pytest.fixture
def open_event(event: Event) -> Event:
    """The event with its check-in window open now."""
    now = timezone.now()
    event.check_in_starts_at = now - timedelta(hours=1)
    event.check_in_ends_at = now + timedelta(hours=1)
    event.save()
    return event


def _check_in(client: Client, event: Event, ticket: Ticket) -> t.Any:
    url = reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "code": ticket.pk})
    return client.post(url, content_type="application/json")


def _change_during_request(ticket: Ticket, **fields: t.Any) -> t.Any:
    """Patch a step between the unlocked read and the write so that another writer commits ``fields`` there."""

    def change_then_continue(event: Event, tier: TicketTier) -> None:
        Ticket.objects.filter(pk=ticket.pk).update(**fields)
        _ensure_check_in_open(event, tier)

    return patch.object(check_in_service, "_ensure_check_in_open", side_effect=change_then_continue)


def test_check_in_refuses_a_ticket_cancelled_during_the_request(
    organization_owner_client: Client, open_event: Event, active_online_ticket: Ticket
) -> None:
    with _change_during_request(active_online_ticket, status=Ticket.TicketStatus.CANCELLED):
        response = _check_in(organization_owner_client, open_event, active_online_ticket)

    assert response.status_code == 400
    assert response.json()["detail"] == "This ticket has been cancelled."
    active_online_ticket.refresh_from_db()
    assert active_online_ticket.status == Ticket.TicketStatus.CANCELLED
    assert active_online_ticket.checked_in_at is None


def test_check_in_refuses_a_ticket_checked_in_by_another_scan_during_the_request(
    organization_owner_client: Client,
    open_event: Event,
    active_online_ticket: Ticket,
    organization_staff_user: RevelUser,
) -> None:
    first_scan_at = timezone.now() - timedelta(minutes=1)
    with _change_during_request(
        active_online_ticket,
        status=Ticket.TicketStatus.CHECKED_IN,
        checked_in_at=first_scan_at,
        checked_in_by=organization_staff_user,
    ):
        response = _check_in(organization_owner_client, open_event, active_online_ticket)

    assert response.status_code == 400
    assert response.json()["detail"] == "This ticket has already been checked in."
    active_online_ticket.refresh_from_db()
    assert active_online_ticket.checked_in_at == first_scan_at
    assert active_online_ticket.checked_in_by == organization_staff_user


def test_check_in_refuses_an_online_ticket_moved_back_to_pending_during_the_request(
    organization_owner_client: Client, open_event: Event, active_online_ticket: Ticket
) -> None:
    with _change_during_request(active_online_ticket, status=Ticket.TicketStatus.PENDING):
        response = _check_in(organization_owner_client, open_event, active_online_ticket)

    assert response.status_code == 400
    assert response.json()["detail"] == "This ticket is pending payment confirmation."
    active_online_ticket.refresh_from_db()
    assert active_online_ticket.status == Ticket.TicketStatus.PENDING


def test_check_in_answers_404_for_a_ticket_deleted_during_the_request(
    organization_owner_client: Client, open_event: Event, pending_offline_ticket: Ticket
) -> None:
    def delete_then_continue(event: Event, tier: TicketTier) -> None:
        Ticket.objects.filter(pk=pending_offline_ticket.pk).delete()
        _ensure_check_in_open(event, tier)

    with patch.object(check_in_service, "_ensure_check_in_open", side_effect=delete_then_continue):
        response = _check_in(organization_owner_client, open_event, pending_offline_ticket)

    assert response.status_code == 404
    assert not Ticket.objects.filter(pk=pending_offline_ticket.pk).exists()


def test_check_in_of_an_offline_ticket_unconfirmed_during_the_request_still_succeeds(
    organization_owner_client: Client, open_event: Event, pending_offline_ticket: Ticket
) -> None:
    """A PENDING offline ticket may be checked in (payment at the door), so the re-check lets it through."""
    Ticket.objects.filter(pk=pending_offline_ticket.pk).update(status=Ticket.TicketStatus.ACTIVE)
    with _change_during_request(pending_offline_ticket, status=Ticket.TicketStatus.PENDING):
        response = _check_in(organization_owner_client, open_event, pending_offline_ticket)

    assert response.status_code == 200
    pending_offline_ticket.refresh_from_db()
    assert pending_offline_ticket.status == Ticket.TicketStatus.CHECKED_IN


def test_check_in_query_count(
    open_event: Event,
    active_online_ticket: Ticket,
    organization_owner_user: RevelUser,
    django_assert_num_queries: t.Any,
) -> None:
    """Pin the cost of the door scan.

    14 queries before #1482. The lock adds 6: SAVEPOINT, the Payment lock, the tier-id read, the tier
    lock, the ticket lock and RELEASE SAVEPOINT.
    """
    with django_assert_num_queries(EXPECTED_CHECK_IN_QUERIES):
        ticket = check_in_service.check_in_ticket(open_event, active_online_ticket.pk, organization_owner_user)

    assert ticket.status == Ticket.TicketStatus.CHECKED_IN


def test_check_in_writes_the_ticket_while_it_holds_the_row_locks(
    open_event: Event, active_online_ticket: Ticket, organization_owner_user: RevelUser
) -> None:
    """The status re-check and the write run under Payment, tier and ticket row locks, in that order.

    The locks are taken with FOR UPDATE inside the same transaction as the UPDATE of the ticket, so a
    competing writer of the same rows waits until check-in commits.
    """
    with CaptureQueriesContext(connection) as ctx:
        check_in_service.check_in_ticket(open_event, active_online_ticket.pk, organization_owner_user)

    sqls = [q["sql"] for q in ctx.captured_queries]
    locked = [m.group(1) for sql in sqls if "FOR UPDATE" in sql and (m := re.search(r'FROM "(\w+)"', sql)) is not None]
    assert locked[:3] == ["events_payment", "events_tickettier", "events_ticket"]
    ticket_lock = next(i for i, sql in enumerate(sqls) if "FOR UPDATE" in sql and 'FROM "events_ticket"' in sql)
    ticket_write = next(i for i, sql in enumerate(sqls) if sql.startswith('UPDATE "events_ticket"'))
    assert ticket_lock < ticket_write
    assert not any(sql.startswith("RELEASE SAVEPOINT") for sql in sqls[ticket_lock:ticket_write])
