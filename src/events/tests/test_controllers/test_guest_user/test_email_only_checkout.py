"""Email-only guest checkout (#845). URL names/payload idioms: mirror test_guest_checkout.py."""

from unittest.mock import Mock, patch

import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import Event, Ticket, TicketTier
from events.schema.guest_checkout import GuestBatchCheckoutPayload, GuestTicketItemPayload
from events.service.guest import get_or_create_guest_user


class TestSchemas:
    def test_guest_payload_valid_with_email_only(self) -> None:
        payload = GuestBatchCheckoutPayload.model_validate({"email": "speedy@example.com", "tickets": [{}]})
        assert payload.first_name == ""
        assert payload.last_name == ""
        assert payload.tickets[0].guest_name is None

    def test_jwt_item_payload_defaults_to_empty_name(self) -> None:
        assert GuestTicketItemPayload().guest_name == ""


@pytest.mark.django_db
class TestEmailOnlyGuest:
    def test_guest_user_created_without_names(self) -> None:
        user = get_or_create_guest_user("speedy@example.com")
        assert user.guest is True
        assert user.first_name == "" and user.last_name == ""


@pytest.mark.django_db(transaction=True)
class TestEmailOnlyGuestCheckout:
    """Guest checkout with no names at all.

    Uses ``transaction=True`` for the same reason as ``TestGuestTicketCheckout``:
    the non-online branch schedules ``send_guest_ticket_confirmation`` on commit.
    """

    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_email_only_checkout_on_relaxed_event(
        self, mock_send_email: Mock, guest_event_with_tickets: Event, free_tier: TicketTier
    ) -> None:
        """Names off: email-only checkout succeeds and confirms into a blank-named ticket."""
        guest_event_with_tickets.require_ticket_names = False
        guest_event_with_tickets.save(update_fields=["require_ticket_names"])

        client = Client()
        url = reverse(
            "api:guest_ticket_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": free_tier.pk},
        )

        response = client.post(
            url,
            data={"email": "speedy@example.com", "tickets": [{}]},
            content_type="application/json",
        )

        assert response.status_code == 200
        mock_send_email.assert_called_once()

        # Free tier is non-online: the ticket only exists after the emailed link is clicked.
        token = mock_send_email.call_args[0][1]
        confirm_response = client.post(
            reverse("api:confirm_guest_action"),
            data={"token": token},
            content_type="application/json",
        )

        assert confirm_response.status_code == 200
        ticket = Ticket.objects.get(event=guest_event_with_tickets, user__email="speedy@example.com")
        assert ticket.guest_name == ""

    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_email_only_checkout_rejected_when_names_required(
        self, mock_send_email: Mock, guest_event_with_tickets: Event, free_tier: TicketTier
    ) -> None:
        """Names required: rejected at request time, before any email is sent."""
        assert guest_event_with_tickets.require_ticket_names is True

        client = Client()
        url = reverse(
            "api:guest_ticket_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": free_tier.pk},
        )

        response = client.post(
            url,
            data={"email": "speedy@example.com", "tickets": [{}]},
            content_type="application/json",
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "This event requires a name on every ticket."
        mock_send_email.assert_not_called()
