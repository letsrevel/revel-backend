"""The ticket compliance lines exposed on the attendee ticket API (#1077)."""

import typing as t
from decimal import Decimal

import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.compliance.enforcement import ticket_fields
from events.models import Organization, Ticket, TicketTier

pytestmark = pytest.mark.django_db

RESERVATION = "Reservation only: this isn't a fiscal access ticket (titolo d'accesso). The organizer issues it."


@pytest.fixture
def ticket_holder_client(member_user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(member_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")  # type: ignore[attr-defined]


def _dashboard_tickets(client: Client) -> list[dict[str, t.Any]]:
    response = client.get(reverse("api:dashboard_tickets"), {"include_past": True})
    assert response.status_code == 200, response.content
    return t.cast(list[dict[str, t.Any]], response.json()["results"])


def test_lines_match_the_pdf_and_wallet_source(
    ticket_holder_client: Client, organization: Organization, ticket: Ticket
) -> None:
    organization.billing_name = "Org Legal Entity Ltd"
    organization.vat_id = "ATU12345678"
    organization.vat_country_code = "AT"
    organization.save()

    [item] = _dashboard_tickets(ticket_holder_client)

    expected = [
        {"key": f.key, "label": f.label, "value": f.value}
        for f in ticket_fields(Ticket.objects.full().get(pk=ticket.pk))
    ]
    assert item["compliance_lines"] == expected
    assert {line["key"] for line in item["compliance_lines"]} >= {"organizer", "tax_id", "price", "notice"}


@pytest.mark.parametrize(("price", "shown"), [(Decimal("10"), True), (Decimal("0"), False)])
def test_italian_reservation_line_only_on_priced_tickets(
    ticket_holder_client: Client, organization: Organization, ticket: Ticket, price: Decimal, shown: bool
) -> None:
    organization.vat_country_code = "IT"
    organization.save(update_fields=["vat_country_code"])
    TicketTier.objects.filter(pk=ticket.tier_id).update(price=price)

    [item] = _dashboard_tickets(ticket_holder_client)

    values = [line["value"] for line in item["compliance_lines"]]
    assert (RESERVATION in values) is shown


def test_dashboard_list_adds_no_queries_per_ticket(
    ticket_holder_client: Client, organization: Organization, ticket: Ticket
) -> None:
    organization.vat_country_code = "IT"
    organization.save(update_fields=["vat_country_code"])

    with CaptureQueriesContext(connection) as one:
        assert len(_dashboard_tickets(ticket_holder_client)) == 1

    for i in range(3):
        Ticket.objects.create(event=ticket.event, user=ticket.user, tier=ticket.tier, guest_name=f"Guest {i}")

    with CaptureQueriesContext(connection) as four:
        assert len(_dashboard_tickets(ticket_holder_client)) == 4

    assert len(four) == len(one)


def test_guest_name_update_response_carries_the_lines(ticket_holder_client: Client, ticket: Ticket) -> None:
    response = ticket_holder_client.patch(
        reverse("api:dashboard_update_ticket_guest_name", kwargs={"ticket_id": ticket.id}),
        data={"guest_name": "New Name"},
        content_type="application/json",
    )

    assert response.status_code == 200, response.content
    assert "notice" in {line["key"] for line in response.json()["compliance_lines"]}
