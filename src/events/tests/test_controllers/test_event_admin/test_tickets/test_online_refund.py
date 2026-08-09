"""Tests for the organizer online-refund endpoints (#865): refund, cancel-with-refund, context."""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
import stripe
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from events.models import (
    Event,
    EventSeries,
    HeldSeriesPass,
    OrganizationStaff,
    Payment,
    Refund,
    SeriesPass,
    Ticket,
    TicketTier,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def online_ticket(
    ticket_factory: t.Callable[..., Ticket],
    tier_factory: t.Callable[..., TicketTier],
) -> Ticket:
    """An ACTIVE ticket on an ONLINE tier, no payment attached yet."""
    tier = tier_factory(payment_method=TicketTier.PaymentMethod.ONLINE, price=Decimal("40.00"))
    return ticket_factory(tier=tier, status=Ticket.TicketStatus.ACTIVE)


@pytest.fixture
def online_paid_ticket(
    online_ticket: Ticket,
    payment_factory: t.Callable[..., Payment],
) -> Ticket:
    """An ACTIVE online ticket with a SUCCEEDED Stripe payment for 40.00 EUR."""
    payment_factory(
        ticket=online_ticket,
        amount=Decimal("40.00"),
        status=Payment.PaymentStatus.SUCCEEDED,
        stripe_payment_intent_id="pi_test_online_refund",
    )
    return online_ticket


# --- TestAdminIssueRefund ---


class TestAdminIssueRefund:
    def test_full_refund_returns_refund_row(
        self, organization_owner_client: Client, event: Event, online_paid_ticket: Ticket
    ) -> None:
        url = reverse(
            "api:refund_ticket_payment",
            kwargs={"event_id": event.pk, "ticket_id": online_paid_ticket.pk},
        )
        with patch("stripe.Refund.create") as mock_create:
            mock_create.return_value.id = "re_full"
            response = organization_owner_client.post(url, data={}, content_type="application/json")

        assert response.status_code == 200, response.content
        data = response.json()
        assert data["amount"] == "40.00"
        assert data["status"] == Refund.RefundStatus.PENDING
        online_paid_ticket.refresh_from_db()
        assert online_paid_ticket.status == Ticket.TicketStatus.ACTIVE

    def test_partial_refund(self, organization_owner_client: Client, event: Event, online_paid_ticket: Ticket) -> None:
        url = reverse(
            "api:refund_ticket_payment",
            kwargs={"event_id": event.pk, "ticket_id": online_paid_ticket.pk},
        )
        with patch("stripe.Refund.create") as mock_create:
            mock_create.return_value.id = "re_partial"
            response = organization_owner_client.post(url, data={"amount": "10.00"}, content_type="application/json")

        assert response.status_code == 200, response.content
        assert response.json()["amount"] == "10.00"

    def test_second_refund_within_remaining(
        self, organization_owner_client: Client, event: Event, online_paid_ticket: Ticket
    ) -> None:
        url = reverse(
            "api:refund_ticket_payment",
            kwargs={"event_id": event.pk, "ticket_id": online_paid_ticket.pk},
        )
        with patch("stripe.Refund.create") as mock_create:
            mock_create.return_value.id = "re_1"
            first = organization_owner_client.post(url, data={"amount": "10.00"}, content_type="application/json")
            mock_create.return_value.id = "re_2"
            second = organization_owner_client.post(url, data={"amount": "30.00"}, content_type="application/json")

        assert first.status_code == 200, first.content
        assert second.status_code == 200, second.content

    def test_over_remaining_400(
        self, organization_owner_client: Client, event: Event, online_paid_ticket: Ticket
    ) -> None:
        url = reverse(
            "api:refund_ticket_payment",
            kwargs={"event_id": event.pk, "ticket_id": online_paid_ticket.pk},
        )
        response = organization_owner_client.post(url, data={"amount": "50.00"}, content_type="application/json")

        assert response.status_code == 400, response.content

    def test_fully_refunded_409(
        self, organization_owner_client: Client, event: Event, online_paid_ticket: Ticket
    ) -> None:
        Refund.objects.create(
            payment=online_paid_ticket.payment,
            amount=Decimal("40.00"),
            currency="EUR",
            status=Refund.RefundStatus.SUCCEEDED,
            source=Refund.Source.ORGANIZER_API,
        )
        url = reverse(
            "api:refund_ticket_payment",
            kwargs={"event_id": event.pk, "ticket_id": online_paid_ticket.pk},
        )
        response = organization_owner_client.post(url, data={}, content_type="application/json")

        assert response.status_code == 409, response.content

    def test_offline_ticket_409(
        self,
        organization_owner_client: Client,
        event: Event,
        offline_tier: TicketTier,
        ticket_factory: t.Callable[..., Ticket],
        payment_factory: t.Callable[..., Payment],
    ) -> None:
        """A recorded (non-Stripe) offline payment has nothing to refund via this endpoint."""
        ticket = ticket_factory(tier=offline_tier, status=Ticket.TicketStatus.ACTIVE)
        payment_factory(ticket=ticket, amount=Decimal("25.00"), stripe_payment_intent_id="")
        url = reverse(
            "api:refund_ticket_payment",
            kwargs={"event_id": event.pk, "ticket_id": ticket.pk},
        )
        response = organization_owner_client.post(url, data={}, content_type="application/json")

        assert response.status_code == 409, response.content

    def test_balance_insufficient_402(
        self, organization_owner_client: Client, event: Event, online_paid_ticket: Ticket
    ) -> None:
        """A Stripe decline must roll back the PENDING Refund row (issue_refund needs an atomic caller).

        Without an explicit ``transaction.atomic()`` around the service call, Ninja Extra's own
        exception handling means the mapped 402 never propagates far enough to trigger
        ATOMIC_REQUESTS' rollback, permanently committing an orphan PENDING row with no
        stripe_refund_id that the webhook can never resolve.
        """
        err = stripe.error.InvalidRequestError(message="insufficient", param=None, code="balance_insufficient")
        url = reverse(
            "api:refund_ticket_payment",
            kwargs={"event_id": event.pk, "ticket_id": online_paid_ticket.pk},
        )
        with patch("stripe.Refund.create", side_effect=err):
            response = organization_owner_client.post(url, data={}, content_type="application/json")

        assert response.status_code == 402, response.content
        assert Refund.objects.count() == 0

    def test_no_payment_409(
        self,
        organization_owner_client: Client,
        event: Event,
        online_ticket: Ticket,
    ) -> None:
        """A ticket with no Payment row at all answers 409 (NothingToRefundError), not a bare 404."""
        url = reverse(
            "api:refund_ticket_payment",
            kwargs={"event_id": event.pk, "ticket_id": online_ticket.pk},
        )
        response = organization_owner_client.post(url, data={}, content_type="application/json")

        assert response.status_code == 409, response.content
        assert "detail" in response.json()

    def test_requires_manage_tickets_permission(
        self,
        organization_staff_client: Client,
        staff_member: OrganizationStaff,
        event: Event,
        online_paid_ticket: Ticket,
    ) -> None:
        perms = staff_member.permissions
        perms["default"]["manage_tickets"] = False
        staff_member.permissions = perms
        staff_member.save()

        url = reverse(
            "api:refund_ticket_payment",
            kwargs={"event_id": event.pk, "ticket_id": online_paid_ticket.pk},
        )
        response = organization_staff_client.post(url, data={}, content_type="application/json")

        assert response.status_code == 403, response.content


# --- TestAdminCancelWithRefund ---


class TestAdminCancelWithRefund:
    def test_online_cancel_without_refund(
        self, organization_owner_client: Client, event: Event, online_paid_ticket: Ticket
    ) -> None:
        online_paid_ticket.tier.quantity_sold = 1
        online_paid_ticket.tier.save(update_fields=["quantity_sold"])

        url = reverse(
            "api:cancel_ticket",
            kwargs={"event_id": event.pk, "ticket_id": online_paid_ticket.pk},
        )
        with patch("stripe.Refund.create") as mock_create:
            response = organization_owner_client.post(url, data={}, content_type="application/json")

        assert response.status_code == 200, response.content
        mock_create.assert_not_called()
        online_paid_ticket.refresh_from_db()
        assert online_paid_ticket.status == Ticket.TicketStatus.CANCELLED
        online_paid_ticket.tier.refresh_from_db()
        assert online_paid_ticket.tier.quantity_sold == 0
        assert Refund.objects.count() == 0

    def test_online_cancel_with_refund_amount(
        self, organization_owner_client: Client, event: Event, online_paid_ticket: Ticket
    ) -> None:
        url = reverse(
            "api:cancel_ticket",
            kwargs={"event_id": event.pk, "ticket_id": online_paid_ticket.pk},
        )
        with patch("stripe.Refund.create") as mock_create:
            mock_create.return_value.id = "re_cancel"
            response = organization_owner_client.post(
                url, data={"refund_amount": "15.00"}, content_type="application/json"
            )

        assert response.status_code == 200, response.content
        online_paid_ticket.refresh_from_db()
        assert online_paid_ticket.status == Ticket.TicketStatus.CANCELLED
        refund = Refund.objects.get(payment=online_paid_ticket.payment)
        assert refund.amount == Decimal("15.00")

    def test_offline_cancel_unchanged(
        self,
        organization_owner_client: Client,
        event: Event,
        pending_offline_ticket: Ticket,
        offline_tier: TicketTier,
    ) -> None:
        offline_tier.quantity_sold = 5
        offline_tier.save(update_fields=["quantity_sold"])

        url = reverse(
            "api:cancel_ticket",
            kwargs={"event_id": event.pk, "ticket_id": pending_offline_ticket.pk},
        )
        response = organization_owner_client.post(url, data={}, content_type="application/json")

        assert response.status_code == 200, response.content
        pending_offline_ticket.refresh_from_db()
        assert pending_offline_ticket.status == Ticket.TicketStatus.CANCELLED
        offline_tier.refresh_from_db()
        assert offline_tier.quantity_sold == 4

    def test_offline_cancel_with_refund_amount_marks_refunded(
        self,
        organization_owner_client: Client,
        event: Event,
        offline_tier: TicketTier,
        ticket_factory: t.Callable[..., Ticket],
        payment_factory: t.Callable[..., Payment],
    ) -> None:
        ticket = ticket_factory(tier=offline_tier, status=Ticket.TicketStatus.ACTIVE)
        payment = payment_factory(
            ticket=ticket,
            amount=Decimal("25.00"),
            stripe_payment_intent_id="",
            status=Payment.PaymentStatus.SUCCEEDED,
        )
        url = reverse(
            "api:cancel_ticket",
            kwargs={"event_id": event.pk, "ticket_id": ticket.pk},
        )
        response = organization_owner_client.post(url, data={"refund_amount": "10.00"}, content_type="application/json")

        assert response.status_code == 200, response.content
        ticket.refresh_from_db()
        payment.refresh_from_db()
        assert ticket.status == Ticket.TicketStatus.CANCELLED
        assert ticket.offline_refund_amount == Decimal("10.00")
        assert payment.status == Payment.PaymentStatus.REFUNDED

    def test_already_cancelled_409(
        self, organization_owner_client: Client, event: Event, online_paid_ticket: Ticket
    ) -> None:
        url = reverse(
            "api:cancel_ticket",
            kwargs={"event_id": event.pk, "ticket_id": online_paid_ticket.pk},
        )
        first = organization_owner_client.post(url, data={}, content_type="application/json")
        assert first.status_code == 200, first.content

        second = organization_owner_client.post(url, data={}, content_type="application/json")
        assert second.status_code == 409, second.content

    def test_stripe_failure_rolls_back_cancellation(
        self, organization_owner_client: Client, event: Event, online_paid_ticket: Ticket
    ) -> None:
        url = reverse(
            "api:cancel_ticket",
            kwargs={"event_id": event.pk, "ticket_id": online_paid_ticket.pk},
        )
        with patch("stripe.Refund.create", side_effect=stripe.error.APIError("boom")):
            response = organization_owner_client.post(
                url, data={"refund_amount": "10.00"}, content_type="application/json"
            )

        assert response.status_code == 502, response.content
        online_paid_ticket.refresh_from_db()
        assert online_paid_ticket.status == Ticket.TicketStatus.ACTIVE
        assert Refund.objects.count() == 0

    def test_online_series_pass_ticket_rejected(
        self,
        organization_owner_client: Client,
        event: Event,
        event_series: EventSeries,
        online_paid_ticket: Ticket,
    ) -> None:
        """A series-pass-materialized online ticket can't be cancelled here — pass endpoints own it."""
        series_pass = SeriesPass.objects.create(
            event_series=event_series,
            name="Season Pass",
            price=Decimal("100.00"),
            pro_rata_discount=Decimal("0"),
        )
        held_pass = HeldSeriesPass.objects.create(
            series_pass=series_pass,
            user=online_paid_ticket.user,
            price_paid=Decimal("100.00"),
            status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
        )
        online_paid_ticket.held_pass = held_pass
        online_paid_ticket.save(update_fields=["held_pass"])

        url = reverse(
            "api:cancel_ticket",
            kwargs={"event_id": event.pk, "ticket_id": online_paid_ticket.pk},
        )
        response = organization_owner_client.post(url, data={}, content_type="application/json")

        assert response.status_code == 400, response.content
        online_paid_ticket.refresh_from_db()
        assert online_paid_ticket.status == Ticket.TicketStatus.ACTIVE


# --- TestRefundContext ---


class TestRefundContext:
    def test_context_for_online_ticket(
        self, organization_owner_client: Client, event: Event, online_paid_ticket: Ticket
    ) -> None:
        # Make the ticket policy-cancellable with a full-refund window so the
        # preview's policy_suggested_amount is non-null.
        event.start = timezone.now() + timedelta(days=5)
        event.end = event.start + timedelta(days=1)
        event.save(update_fields=["start", "end"])
        online_paid_ticket.tier.allow_user_cancellation = True
        online_paid_ticket.tier.save(update_fields=["allow_user_cancellation"])
        online_paid_ticket.refund_policy_snapshot = {
            "tiers": [{"hours_before_event": 0, "refund_percentage": "100.00"}],
            "flat_fee": "0.00",
        }
        online_paid_ticket.save(update_fields=["refund_policy_snapshot"])

        url = reverse(
            "api:ticket_refund_context",
            kwargs={"event_id": event.pk, "ticket_id": online_paid_ticket.pk},
        )
        response = organization_owner_client.get(url)

        assert response.status_code == 200, response.content
        data = response.json()
        assert data["payment_method"] == TicketTier.PaymentMethod.ONLINE
        assert data["amount_paid"] == "40.00"
        assert data["currency"] == "EUR"
        assert Decimal(data["total_refunded"]) == Decimal("0")
        assert Decimal(data["remaining_refundable"]) == Decimal("40.00")
        assert Decimal(data["policy_suggested_amount"]) == Decimal("40.00")
        assert data["refunds"] == []

    def test_context_lists_previous_refunds(
        self, organization_owner_client: Client, event: Event, online_paid_ticket: Ticket
    ) -> None:
        Refund.objects.create(
            payment=online_paid_ticket.payment,
            amount=Decimal("10.00"),
            currency="EUR",
            status=Refund.RefundStatus.SUCCEEDED,
            source=Refund.Source.ORGANIZER_API,
        )
        Refund.objects.create(
            payment=online_paid_ticket.payment,
            amount=Decimal("5.00"),
            currency="EUR",
            status=Refund.RefundStatus.PENDING,
            source=Refund.Source.ORGANIZER_API,
        )

        url = reverse(
            "api:ticket_refund_context",
            kwargs={"event_id": event.pk, "ticket_id": online_paid_ticket.pk},
        )
        response = organization_owner_client.get(url)

        assert response.status_code == 200, response.content
        data = response.json()
        assert data["total_refunded"] == "10.00"
        assert data["total_pending"] == "5.00"
        assert data["remaining_refundable"] == "25.00"
        assert len(data["refunds"]) == 2
