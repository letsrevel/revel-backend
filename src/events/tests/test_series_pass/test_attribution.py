"""Series-pass attribution (#922).

A pass's tickets are materialised at purchase, on activation backfill and on series
extension. Only the purchase sees the checkout request, so the tags live on the
``HeldSeriesPass`` and every materialisation copies them onto its tickets.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    Event,
    EventSeries,
    HeldSeriesPass,
    Organization,
    SeriesPass,
    SeriesPassTierLink,
    Ticket,
    TicketAttribution,
    TicketTier,
)
from events.service import series_pass_service
from events.service.series_pass_purchase import SeriesPassPurchaseService
from events.tasks.series_pass import materialize_series_pass_holders

pytestmark = pytest.mark.django_db

ATTRIBUTION: TicketAttribution = {"utm_source": "instagram", "utm_medium": "social", "utm_content": "example.org"}


def _link_event(organization: Organization, series_pass: SeriesPass, slug: str, days: int) -> SeriesPassTierLink:
    event = Event.objects.create(
        organization=organization,
        name=f"Event {slug}",
        slug=slug,
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        event_series=series_pass.event_series,
        max_attendees=100,
        start=timezone.now() + timedelta(days=days),
        status=Event.EventStatus.OPEN,
        requires_ticket=True,
    )
    tier = TicketTier.objects.create(
        event=event, name=f"Tier {slug}", price=Decimal("10.00"), currency="EUR", payment_method="online"
    )
    return SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=tier)


@pytest.fixture
def free_pass(organization: Organization, event_series: EventSeries) -> SeriesPass:
    """A public, free pass covering two future events (the quote requires >= 2 remaining)."""
    organization.visibility = Organization.Visibility.PUBLIC
    organization.save(update_fields=["visibility"])
    series_pass = SeriesPass.objects.create(
        event_series=event_series,
        name="Free Season",
        price=Decimal("0.00"),
        pro_rata_discount=Decimal("0.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
        visibility=SeriesPass.Visibility.PUBLIC,
    )
    for i in range(2):
        _link_event(organization, series_pass, f"free-{i}", i + 1)
    return series_pass


def test_purchase_stamps_pass_and_every_ticket(free_pass: SeriesPass, revel_user: RevelUser) -> None:
    result = SeriesPassPurchaseService(free_pass, revel_user).purchase(attribution=ATTRIBUTION)
    assert isinstance(result, HeldSeriesPass)

    result.refresh_from_db()
    assert result.attribution == ATTRIBUTION
    tickets = list(Ticket.objects.filter(held_pass=result))
    assert len(tickets) == 2
    assert all(ticket.attribution == ATTRIBUTION for ticket in tickets)


def test_purchase_without_attribution_stays_null(free_pass: SeriesPass, revel_user: RevelUser) -> None:
    result = SeriesPassPurchaseService(free_pass, revel_user).purchase()
    assert isinstance(result, HeldSeriesPass)

    result.refresh_from_db()
    assert result.attribution is None
    assert all(ticket.attribution is None for ticket in Ticket.objects.filter(held_pass=result))


def test_backfill_copies_pass_attribution(
    organization: Organization, free_pass: SeriesPass, revel_user: RevelUser
) -> None:
    held_pass = HeldSeriesPass.objects.create(
        series_pass=free_pass,
        user=revel_user,
        price_paid=Decimal("0.00"),
        status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
        attribution=ATTRIBUTION,
    )
    created = series_pass_service.backfill_missing_tickets(held_pass)

    assert len(created) == 2
    assert all(Ticket.objects.get(pk=ticket.pk).attribution == ATTRIBUTION for ticket in created)


def test_extension_copies_pass_attribution(
    organization: Organization, free_pass: SeriesPass, revel_user: RevelUser
) -> None:
    held_pass = HeldSeriesPass.objects.create(
        series_pass=free_pass,
        user=revel_user,
        price_paid=Decimal("0.00"),
        status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
        attribution=ATTRIBUTION,
    )
    new_link = _link_event(organization, free_pass, "extension", 10)

    with patch("notifications.signals.series_pass.send_series_pass_extended"):
        materialize_series_pass_holders(str(free_pass.id), [str(new_link.event_id)])

    ticket = Ticket.objects.get(held_pass=held_pass, event_id=new_link.event_id)
    assert ticket.attribution == ATTRIBUTION


# ---- Endpoint: the checkout body is now a wrapper (#922, breaking for the FE) ----


@pytest.fixture
def holder_client(revel_user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(revel_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


def _checkout_url(series_pass: SeriesPass) -> str:
    return reverse("api:checkout_series_pass", kwargs={"pass_id": series_pass.id})


def test_checkout_endpoint_accepts_attribution(
    holder_client: Client, free_pass: SeriesPass, revel_user: RevelUser
) -> None:
    body = {"billing_info": {"billing_name": "Gia Holder"}, "attribution": ATTRIBUTION}
    response = holder_client.post(_checkout_url(free_pass), data=body, content_type="application/json")
    assert response.status_code == 200, response.content

    held_pass = HeldSeriesPass.objects.get(series_pass=free_pass, user=revel_user)
    assert held_pass.attribution == ATTRIBUTION
    assert all(ticket.attribution == ATTRIBUTION for ticket in Ticket.objects.filter(held_pass=held_pass))


def test_checkout_endpoint_still_accepts_an_empty_body(
    holder_client: Client, free_pass: SeriesPass, revel_user: RevelUser
) -> None:
    response = holder_client.post(_checkout_url(free_pass), data=b"", content_type="application/json")
    assert response.status_code == 200, response.content
    assert HeldSeriesPass.objects.get(series_pass=free_pass, user=revel_user).attribution is None


def test_checkout_endpoint_rejects_the_legacy_bare_billing_body(holder_client: Client, free_pass: SeriesPass) -> None:
    """A stale FE posting the pre-#922 shape must fail loudly, not silently drop its billing info."""
    response = holder_client.post(
        _checkout_url(free_pass), data={"billing_name": "Gia Holder"}, content_type="application/json"
    )
    assert response.status_code == 422, response.content
    assert not HeldSeriesPass.objects.filter(series_pass=free_pass).exists()
