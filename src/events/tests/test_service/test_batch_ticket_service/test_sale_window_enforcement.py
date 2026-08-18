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
from unittest.mock import Mock, patch

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


@pytest.fixture
def guest_event(organization: Organization) -> Event:
    """Same as ``event``, but open to unauthenticated (guest) checkout."""
    return Event.objects.create(
        organization=organization,
        name="Guest Sale Window Event",
        slug="guest-sale-window-event",
        event_type=Event.EventType.PUBLIC,
        start=timezone.now() + timedelta(days=7),
        status=Event.EventStatus.OPEN,
        visibility=Event.Visibility.PUBLIC,
        require_ticket_names=False,
        can_attend_without_login=True,
        max_attendees=100,
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


class TestGuestSaleWindowEnforcement:
    """The guest cart's non-online branch defers create_batch — the sale window's
    other gate — to the emailed confirmation click, so the window has to be answered
    at the initial request or the buyer gets a 200 and a link that 403s.

    ``transaction=True`` on both tests because the assertion is about the
    confirmation email, whose dispatch is registered with ``transaction.on_commit``
    and so never fires under pytest-django's wrapping transaction.
    """

    @pytest.mark.django_db(transaction=True)
    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_closed_window_rejected_at_checkout_and_no_email(self, mock_send_email: Mock, guest_event: Event) -> None:
        # A sibling tier still on sale, so the any-tier eligibility gate passes and
        # only the per-tier window can reject the cart.
        TicketTier.objects.create(
            event=guest_event,
            name="Open Sibling",
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        closed_tier = TicketTier.objects.create(
            event=guest_event,
            name="Closed Tier",
            payment_method=TicketTier.PaymentMethod.FREE,
            sales_end_at=timezone.now() - timedelta(days=1),
        )
        payload = {
            "email": "closedwindow@example.com",
            "first_name": "Guest",
            "last_name": "Closed",
            "items": [{"tier_id": str(closed_tier.id), "tickets": [{"guest_name": "Guest Closed"}]}],
        }

        response = Client().post(
            reverse("api:guest_multi_tier_checkout", kwargs={"event_id": guest_event.pk}),
            data=payload,
            content_type="application/json",
        )

        assert response.status_code == 403, response.content
        assert "outside of the sale window" in response.json()["detail"]
        mock_send_email.assert_not_called()

    @pytest.mark.django_db(transaction=True)
    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_open_window_still_sends_confirmation(self, mock_send_email: Mock, guest_event: Event) -> None:
        open_tier = TicketTier.objects.create(
            event=guest_event,
            name="Open Tier",
            payment_method=TicketTier.PaymentMethod.FREE,
            sales_start_at=timezone.now() - timedelta(days=1),
            sales_end_at=timezone.now() + timedelta(days=1),
        )
        payload = {
            "email": "openwindow@example.com",
            "first_name": "Guest",
            "last_name": "Open",
            "items": [{"tier_id": str(open_tier.id), "tickets": [{"guest_name": "Guest Open"}]}],
        }

        response = Client().post(
            reverse("api:guest_multi_tier_checkout", kwargs={"event_id": guest_event.pk}),
            data=payload,
            content_type="application/json",
        )

        assert response.status_code == 200, response.content
        assert response.json()["message"]
        mock_send_email.assert_called_once()
