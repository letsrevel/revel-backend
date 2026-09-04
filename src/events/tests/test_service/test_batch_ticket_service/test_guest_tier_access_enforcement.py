"""Tests for guest-checkout enforcement of the tier's ``purchasable_by`` rule.

Production incident (2026-09-04): an organizer set a tier to INVITED on an event
open to guest checkout. The guest cart's non-online branch defers ``create_batch``
— where ``_assert_purchasable_by`` runs — to the emailed confirmation click, so
every guest got a 200 and a confirmation email, and every click on the link then
403'd. The initial checkout request has to answer the rule up front.
"""

import typing as t
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from events.models import Event, EventInvitation, Organization, PendingEventInvitation, TicketTier

pytestmark = pytest.mark.django_db


@pytest.fixture
def guest_event(organization: Organization) -> Event:
    """Future-dated public event open to unauthenticated (guest) checkout."""
    return Event.objects.create(
        organization=organization,
        name="Guest Tier Access Event",
        slug="guest-tier-access-event",
        event_type=Event.EventType.PUBLIC,
        start=timezone.now() + timedelta(days=7),
        status=Event.EventStatus.OPEN,
        visibility=Event.Visibility.PUBLIC,
        require_ticket_names=False,
        can_attend_without_login=True,
        max_attendees=100,
    )


@pytest.fixture
def invited_tier(guest_event: Event) -> TicketTier:
    """Offline (non-online) tier restricted to invited buyers — the incident's shape."""
    return TicketTier.objects.create(
        event=guest_event,
        name="Invited Only",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        purchasable_by=TicketTier.PurchasableBy.INVITED,
    )


def _checkout(event: Event, tier: TicketTier, email: str) -> t.Any:
    return Client().post(
        reverse("api:guest_multi_tier_checkout", kwargs={"event_id": event.pk}),
        data={
            "email": email,
            "first_name": "Guest",
            "last_name": "Buyer",
            "items": [{"tier_id": str(tier.id), "tickets": [{"guest_name": "Guest Buyer"}]}],
        },
        content_type="application/json",
    )


class TestGuestTierAccessEnforcement:
    """``transaction=True`` because the assertion is about the confirmation email,
    whose dispatch is registered with ``transaction.on_commit`` and so never fires
    under pytest-django's wrapping transaction.
    """

    @pytest.mark.django_db(transaction=True)
    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_uninvited_guest_rejected_at_checkout_and_no_email(
        self, mock_send_email: Mock, guest_event: Event, invited_tier: TicketTier
    ) -> None:
        response = _checkout(guest_event, invited_tier, "uninvited@example.com")

        assert response.status_code == 403, response.content
        assert "not allowed to purchase from this tier" in response.json()["detail"]
        mock_send_email.assert_not_called()

    @pytest.mark.django_db(transaction=True)
    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_email_invited_guest_still_gets_confirmation(
        self, mock_send_email: Mock, guest_event: Event, invited_tier: TicketTier
    ) -> None:
        # The organizer invited this address before it had an account: the pending
        # invitation is converted to a real one when the guest user row is created,
        # so the rule must be evaluated only after that conversion.
        PendingEventInvitation.objects.create(event=guest_event, email="invited@example.com")

        response = _checkout(guest_event, invited_tier, "invited@example.com")

        assert response.status_code == 200, response.content
        assert response.json()["message"]
        assert EventInvitation.objects.filter(event=guest_event, user__email="invited@example.com").exists()
        mock_send_email.assert_called_once()

    @pytest.mark.django_db(transaction=True)
    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_tier_linked_invitation_is_honoured(
        self, mock_send_email: Mock, guest_event: Event, invited_tier: TicketTier
    ) -> None:
        invited_tier.restrict_purchase_to_linked_invitations = True
        invited_tier.save(update_fields=["restrict_purchase_to_linked_invitations"])
        other_tier = TicketTier.objects.create(
            event=guest_event,
            name="Other Invited",
            payment_method=TicketTier.PaymentMethod.OFFLINE,
            purchasable_by=TicketTier.PurchasableBy.INVITED,
        )
        pending = PendingEventInvitation.objects.create(event=guest_event, email="linked@example.com")
        pending.tiers.set([other_tier])

        response = _checkout(guest_event, invited_tier, "linked@example.com")

        assert response.status_code == 403, response.content
        assert "not allowed to purchase from this tier" in response.json()["detail"]
        mock_send_email.assert_not_called()
