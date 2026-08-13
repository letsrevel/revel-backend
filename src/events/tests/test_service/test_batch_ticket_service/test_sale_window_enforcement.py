"""Tests for service-side sale-window enforcement (#846).

CanPurchaseTicket.has_object_permission held the sale-window check, but it never
actually ran on the checkout endpoints: object permissions only fire via
get_object_or_exception, and both checkout routes fetch the tier with
get_object_or_404 instead. TicketSalesGate (EventManager) is any-tier, not
per-tier. So a direct create_batch call against a closed-window tier previously
succeeded — a confirmed bug. _assert_sale_window closes the gap by making the
service itself authoritative.
"""

from datetime import timedelta

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja.errors import HttpError
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Event, Organization, TicketTier
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service import BatchTicketService

pytestmark = pytest.mark.django_db


@pytest.fixture
def event(organization: Organization) -> Event:
    """Future-dated public event; require_ticket_names off so only the sale
    window is under test.
    """
    return Event.objects.create(
        organization=organization,
        name="Sale Window Event",
        slug="sale-window-event",
        event_type=Event.EventType.PUBLIC,
        start=timezone.now() + timedelta(days=7),
        status=Event.EventStatus.OPEN,
        visibility=Event.Visibility.PUBLIC,
        require_ticket_names=False,
    )


class TestSaleWindowEnforcement:
    """Direct create_batch calls against tiers with various sale windows."""

    def test_closed_window_in_the_past_is_rejected(self, event: Event, member_user: RevelUser) -> None:
        tier = TicketTier.objects.create(
            event=event,
            name="Closed Tier",
            payment_method=TicketTier.PaymentMethod.FREE,
            sales_end_at=timezone.now() - timedelta(days=1),
        )
        service = BatchTicketService(event, tier, member_user)
        with pytest.raises(HttpError) as exc:
            service.create_batch([TicketPurchaseItem()])
        assert exc.value.status_code == 403
        assert "outside of the sale window" in str(exc.value.message)

    def test_windowless_tier_passes(self, event: Event, member_user: RevelUser) -> None:
        tier = TicketTier.objects.create(
            event=event,
            name="Open Tier",
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        service = BatchTicketService(event, tier, member_user)
        result = service.create_batch([TicketPurchaseItem()])
        assert isinstance(result, list)
        assert len(result) == 1

    def test_not_yet_open_tier_is_rejected(self, event: Event, member_user: RevelUser) -> None:
        tier = TicketTier.objects.create(
            event=event,
            name="Future Tier",
            payment_method=TicketTier.PaymentMethod.FREE,
            sales_start_at=timezone.now() + timedelta(days=1),
        )
        service = BatchTicketService(event, tier, member_user)
        with pytest.raises(HttpError) as exc:
            service.create_batch([TicketPurchaseItem()])
        assert exc.value.status_code == 403
        assert "outside of the sale window" in str(exc.value.message)


class TestSaleWindowEnforcementEndpoint:
    """The behavior change: this endpoint used to ignore the sale window entirely."""

    def test_checkout_endpoint_rejects_closed_window(self, event: Event, member_user: RevelUser) -> None:
        tier = TicketTier.objects.create(
            event=event,
            name="Closed Tier",
            payment_method=TicketTier.PaymentMethod.FREE,
            sales_end_at=timezone.now() - timedelta(days=1),
        )
        refresh = RefreshToken.for_user(member_user)
        client = Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]
        url = reverse("api:ticket_checkout", kwargs={"event_id": event.pk, "tier_id": tier.pk})

        response = client.post(url, data={"tickets": [{"guest_name": "Test"}]}, content_type="application/json")

        assert response.status_code == 403, response.content
        assert "outside of the sale window" in response.json()["detail"]
