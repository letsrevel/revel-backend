"""``AdminTicketSchema.membership_record``: the record THIS ticket produced, if any."""

import typing as t
from datetime import timedelta

import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, MembershipRecord, MembershipRecordEndedReason, Ticket, TicketTier

pytestmark = pytest.mark.django_db


def _make_ticket(user: RevelUser, event: Event, tier: TicketTier) -> Ticket:
    return Ticket.objects.create(
        guest_name="Guest", user=user, event=event, tier=tier, status=Ticket.TicketStatus.ACTIVE
    )


def _make_record(
    ticket: Ticket, *, ended_reason: MembershipRecordEndedReason | None = None, valid_until: t.Any = None
) -> MembershipRecord:
    return MembershipRecord.objects.create(
        organization=ticket.event.organization,
        user=ticket.user,
        full_name="Dana Day",
        kind=MembershipRecord.Kind.TEMPORARY,
        tier_name="Day Member",
        valid_from=timezone.now(),
        valid_until=valid_until,
        ended_reason=ended_reason,
        source=MembershipRecord.Source.TICKET,
        ticket=ticket,
    )


def _list(client: Client, event: Event) -> dict[str, dict[str, t.Any]]:
    response = client.get(reverse("api:list_tickets", kwargs={"event_id": event.pk}))
    assert response.status_code == 200
    return {row["id"]: row for row in response.json()["results"]}


def test_record_shown_and_null_when_none(
    organization_owner_client: Client,
    event: Event,
    offline_tier: TicketTier,
    public_user: RevelUser,
    member_user: RevelUser,
) -> None:
    """A ticket that produced a record exposes it; a sibling ticket reads null (list and detail)."""
    granting = _make_ticket(public_user, event, offline_tier)
    plain = _make_ticket(member_user, event, offline_tier)
    until = timezone.now() + timedelta(days=1)
    record = _make_record(granting, valid_until=until)

    rows = _list(organization_owner_client, event)
    assert rows[str(granting.pk)]["membership_record"] == {
        "id": str(record.pk),
        "kind": "temporary",
        "tier_name": "Day Member",
        "valid_until": rows[str(granting.pk)]["membership_record"]["valid_until"],
        "ended_reason": None,
    }
    assert rows[str(plain.pk)]["membership_record"] is None

    detail = organization_owner_client.get(
        reverse("api:get_ticket", kwargs={"event_id": event.pk, "ticket_id": granting.pk})
    )
    assert detail.status_code == 200
    assert detail.json()["membership_record"]["id"] == str(record.pk)
    detail = organization_owner_client.get(
        reverse("api:get_ticket", kwargs={"event_id": event.pk, "ticket_id": plain.pk})
    )
    assert detail.json()["membership_record"] is None


def test_revoked_record_still_returned(
    organization_owner_client: Client, event: Event, offline_tier: TicketTier, public_user: RevelUser
) -> None:
    """REVOKED records are returned with ended_reason=revoked so the client can decide what to show."""
    ticket = _make_ticket(public_user, event, offline_tier)
    _make_record(
        ticket, ended_reason=MembershipRecord.EndedReason.REVOKED, valid_until=timezone.now() + timedelta(hours=1)
    )

    record = _list(organization_owner_client, event)[str(ticket.pk)]["membership_record"]
    assert record["ended_reason"] == "revoked"


def test_query_count_constant(
    organization_owner_client: Client, event: Event, offline_tier: TicketTier, public_user: RevelUser
) -> None:
    """The record is annotated, not fetched per row: more tickets do not add queries."""
    first = _make_ticket(public_user, event, offline_tier)
    _make_record(first)
    url = reverse("api:list_tickets", kwargs={"event_id": event.pk})

    with CaptureQueriesContext(connection) as small:
        assert organization_owner_client.get(url).status_code == 200

    for i in range(4):
        user = RevelUser.objects.create_user(username=f"extra{i}@example.com", email=f"extra{i}@example.com")
        _make_record(_make_ticket(user, event, offline_tier))

    with CaptureQueriesContext(connection) as large:
        response = organization_owner_client.get(url)
    assert response.status_code == 200
    assert len(response.json()["results"]) == 5
    assert len(large) == len(small)
