"""Regression tests for the PR #1069 code-review follow-ups (EU layer 1)."""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import orjson
import pytest
from django.contrib.gis.geos import Point
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events import schema
from events.compliance import (
    AttendeeInvoicingCapability,
    BuyerContext,
    Nexus,
    get_policy_for_country,
    registered_policies,
)
from events.exceptions import CountryComplianceError
from events.models import Event, EventSeries, Organization, Ticket, TicketTier
from events.models.attendee_invoice import AttendeeInvoice
from events.service import guest as guest_service
from events.service import ticket_service
from events.service.attendee_invoice_service import generate_attendee_invoice
from events.tests.test_attendee_invoice._helpers import (
    MOCK_RENDER_PDF,
    _create_payment,
    _default_billing_snapshot,
    _make_org_invoicing_ready,
)
from events.tests.test_controllers.test_error_response_contracts import assert_detail_body_with_status
from events.tests.test_series_pass.test_backfill import _make_pass
from geo.models import City

pytestmark = pytest.mark.django_db


def _connected(org: Organization, country: str) -> Organization:
    org.vat_country_code = country
    org.billing_name = "Org Legal Entity"
    org.billing_address = "Main Street 1"
    org.stripe_account_id = "acct_followups"
    org.stripe_charges_enabled = True
    org.stripe_details_submitted = True
    org.save()
    return org


@pytest.fixture
def open_event(organization: Organization) -> Event:
    return Event.objects.create(
        organization=organization,
        name="Gig",
        slug="gig",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        status=Event.EventStatus.OPEN,
        start=timezone.now() + timedelta(days=7),
        end=timezone.now() + timedelta(days=8),
        max_tickets_per_user=5,
        can_attend_without_login=True,
        requires_ticket=True,
    )


def _paused_online(event: Event) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name="Card",
        price=Decimal("20.00"),
        payment_method=TicketTier.PaymentMethod.ONLINE,
        sales_paused=True,
    )


# --- 3. Resuming a blocked tier -------------------------------------------------------


class TestResumeSales:
    def test_resuming_a_blocked_online_tier_is_refused(self, organization: Organization, open_event: Event) -> None:
        _connected(organization, "IT")
        tier = _paused_online(open_event)

        with pytest.raises(CountryComplianceError):
            ticket_service.update_ticket_tier(tier, schema.TicketTierUpdateSchema(sales_paused=False))  # type: ignore[call-arg]

        tier.refresh_from_db()
        assert tier.sales_paused

    def test_resume_endpoint_answers_422(
        self, owner_client: Client, organization: Organization, open_event: Event
    ) -> None:
        _connected(organization, "IT")
        tier = _paused_online(open_event)
        url = reverse("api:update_ticket_tier", kwargs={"event_id": open_event.pk, "tier_id": tier.pk})

        response = owner_client.put(url, data=orjson.dumps({"sales_paused": False}), content_type="application/json")

        assert_detail_body_with_status(response, 422)

    def test_switch_to_door_then_resume(self, organization: Organization, open_event: Event) -> None:
        _connected(organization, "IT")
        tier = _paused_online(open_event)

        ticket_service.update_ticket_tier(
            tier,
            schema.TicketTierUpdateSchema(payment_method=TicketTier.PaymentMethod.AT_THE_DOOR),  # type: ignore[call-arg]
        )
        resumed = ticket_service.update_ticket_tier(
            TicketTier.objects.get(pk=tier.pk),
            schema.TicketTierUpdateSchema(sales_paused=False),  # type: ignore[call-arg]
        )

        assert not resumed.sales_paused

    def test_switch_and_resume_in_one_payload(self, organization: Organization, open_event: Event) -> None:
        _connected(organization, "IT")
        tier = _paused_online(open_event)

        resumed = ticket_service.update_ticket_tier(
            tier,
            schema.TicketTierUpdateSchema(  # type: ignore[call-arg]
                payment_method=TicketTier.PaymentMethod.AT_THE_DOOR, sales_paused=False
            ),
        )

        assert resumed.payment_method == TicketTier.PaymentMethod.AT_THE_DOOR
        assert not resumed.sales_paused

    def test_resume_where_not_blocked(self, organization: Organization, open_event: Event) -> None:
        _connected(organization, "AT")
        tier = _paused_online(open_event)

        resumed = ticket_service.update_ticket_tier(tier, schema.TicketTierUpdateSchema(sales_paused=False))  # type: ignore[call-arg]

        assert not resumed.sales_paused


# --- 4. Invoice idempotency before the gate -------------------------------------------


@patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
def test_existing_invoice_is_returned_even_if_the_gate_now_refuses(
    _pdf: t.Any, organization: Organization, event: Event, event_ticket_tier: TicketTier, member_user: RevelUser
) -> None:
    _make_org_invoicing_ready(organization)
    organization.vat_country_code = "AT"
    organization.vat_id = "ATU12345678"
    organization.invoicing_mode = Organization.InvoicingMode.AUTO
    organization.save()
    _create_payment(
        user=member_user, event=event, tier=event_ticket_tier, buyer_billing_snapshot=_default_billing_snapshot()
    )
    first = generate_attendee_invoice("cs_test_123")
    assert first is not None

    event.vat_country_code = "HU"  # now venue-bound by Hungary's invoicing rule
    event.save(update_fields=["vat_country_code"])

    assert generate_attendee_invoice("cs_test_123") == first
    assert AttendeeInvoice.objects.filter(stripe_session_id="cs_test_123").count() == 1


# --- 5. Capability alignment ----------------------------------------------------------


def test_invoicing_capability_blocked_exactly_when_enabling_is_refused() -> None:
    """``attendee_invoicing_capability`` uses the same establishment probe as the opt-in gate."""
    for code in registered_policies():
        policy = get_policy_for_country(code)
        refused = not policy.attendee_invoicing(BuyerContext(), frozenset({Nexus.ESTABLISHMENT})).allowed
        assert (policy.attendee_invoicing_capability() == AttendeeInvoicingCapability.BLOCKED) is refused, code


# --- 6. Declared 422s -------------------------------------------------------------------


def test_series_pass_checkout_answers_422_for_online_pass_in_italy(
    organization: Organization, event_series: EventSeries, member_user: RevelUser
) -> None:
    _connected(organization, "IT")
    Organization.objects.filter(pk=organization.pk).update(visibility=Organization.Visibility.PUBLIC)
    series_pass = _make_pass(organization, event_series, TicketTier.PaymentMethod.ONLINE, "it-pass")
    client = Client(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(member_user).access_token}")  # type: ignore[attr-defined]

    response = client.post(
        reverse("api:checkout_series_pass", kwargs={"pass_id": series_pass.pk}), content_type="application/json"
    )

    assert_detail_body_with_status(response, 422)


def test_guest_confirm_answers_422_after_tier_switched_to_online(
    organization: Organization, open_event: Event, django_user_model: type[RevelUser]
) -> None:
    _connected(organization, "IT")
    tier = TicketTier.objects.create(
        event=open_event, name="Door", price=Decimal("10.00"), payment_method=TicketTier.PaymentMethod.OFFLINE
    )
    guest = django_user_model.objects.create_user(
        username="guest@example.com", email="guest@example.com", password="", guest=True
    )
    token = guest_service.create_guest_ticket_token(
        guest, open_event.id, tier.id, [schema.TicketPurchaseItem(guest_name="Guest")]
    )
    TicketTier.objects.filter(pk=tier.pk).update(payment_method=TicketTier.PaymentMethod.ONLINE)

    response = Client().post(
        reverse("api:confirm_guest_action"), data={"token": token}, content_type="application/json"
    )

    assert_detail_body_with_status(response, 422)


# --- 7. Full saves keep the ticket number ---------------------------------------------


def test_full_save_of_a_stale_instance_keeps_the_number(ticket: Ticket) -> None:
    stale = Ticket.objects.get(pk=ticket.pk)
    Ticket.objects.filter(pk=ticket.pk).update(ticket_series="", ticket_number=None, issued_at=None)
    stale = Ticket.objects.get(pk=ticket.pk)  # loaded unnumbered
    Ticket.objects.filter(pk=ticket.pk).update(ticket_series="ORG", ticket_number=42, issued_at=timezone.now())

    stale.guest_name = "Renamed in the admin"
    stale.save()  # bare save, as the Django/Unfold admin's save_model does

    ticket.refresh_from_db()
    assert (ticket.ticket_series, ticket.ticket_number) == ("ORG", 42)
    assert ticket.issued_at is not None
    assert ticket.guest_name == "Renamed in the admin"


def test_full_save_of_a_numbered_instance_is_unchanged(ticket: Ticket) -> None:
    ticket.guest_name = "Renamed"
    ticket.save()

    ticket.refresh_from_db()
    assert ticket.ticket_number == 1
    assert ticket.guest_name == "Renamed"


# --- 8. Query counts -------------------------------------------------------------------


def test_generate_events_compliance_adds_no_per_event_queries(
    owner_client: Client, organization: Organization, event_series: EventSeries
) -> None:
    """Serializing generated events resolves compliance (org + venue cities) without per-event queries.

    The endpoint still has pre-existing per-event queries from other EventDetailSchema fields
    (series, series organization, waitlist count, bookmark), which are out of scope here; the
    re-fetch through ``Event.objects.full()`` keeps the compliance resolution off that list.
    """
    city = City.objects.create(
        name="Vienna",
        ascii_name="Vienna",
        country="Austria",
        iso2="AT",
        iso3="AUT",
        city_id=91002,
        location=Point(16.37, 48.21),
    )
    Organization.objects.filter(pk=organization.pk).update(city=city)
    url = reverse("api:generate_series_events", kwargs={"slug": organization.slug, "series_id": event_series.pk})
    events = [
        Event.objects.create(
            organization=organization,
            event_series=event_series,
            name=f"Occurrence {i}",
            slug=f"occurrence-{i}",
            start=timezone.now() + timedelta(days=i + 1),
        )
        for i in range(3)
    ]

    city_queries = []
    for subset in (events[:1], events):
        with (
            patch("events.service.recurrence_service.generate_series_events", return_value=subset),
            CaptureQueriesContext(connection) as ctx,
        ):
            response = owner_client.post(url, content_type="application/json")
        assert response.status_code == 200, response.content
        assert all("compliance" in e for e in response.json())
        city_queries.append(sum('FROM "geo_city"' in q["sql"] for q in ctx.captured_queries))

    # The org's country resolves from its city (no VAT country set): selected, never queried.
    assert city_queries == [0, 0]
