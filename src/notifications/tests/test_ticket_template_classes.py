"""Tests for ticket notification template classes."""

import typing as t
from unittest.mock import MagicMock, patch

import pytest
from django.utils import translation

from accounts.models import RevelUser
from events.models import Ticket
from events.models.ticket import CancellationSource
from notifications.enums import NotificationType
from notifications.service.templates.registry import get_template
from notifications.service.templates.ticket_templates import (
    PaymentConfirmationTemplate,
    TicketCheckedInTemplate,
    TicketCreatedTemplate,
    TicketUpdatedTemplate,
)
from notifications.tests.conftest import _create_notification_for_test

pytestmark = pytest.mark.django_db


# --- Template Class Tests ---


class TestTicketCreatedTemplate:
    """Tests for TicketCreatedTemplate."""

    def test_get_in_app_title_for_ticket_holder(
        self,
        ticket_holder: RevelUser,
    ) -> None:
        """Should return appropriate title for ticket holder."""
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.TICKET_CREATED,
            context={
                "event_name": "Ticket Event",
                "ticket_status": "active",
            },
        )
        template = TicketCreatedTemplate()

        title = template.get_in_app_title(notification)

        assert "Ticket Event" in title
        assert "Confirmed" in title

    def test_get_in_app_title_for_pending_ticket(
        self,
        ticket_holder: RevelUser,
    ) -> None:
        """Should return pending title for pending ticket."""
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.TICKET_CREATED,
            context={
                "event_name": "Ticket Event",
                "ticket_status": "pending",
            },
        )
        template = TicketCreatedTemplate()

        title = template.get_in_app_title(notification)

        assert "Pending" in title

    def test_buyer_title_ignores_holder_name_in_context(
        self,
        ticket_holder: RevelUser,
    ) -> None:
        """The buyer template no longer branches on ``ticket_holder_name`` (staff have their own type)."""
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.TICKET_CREATED,
            context={
                "event_name": "Ticket Event",
                "ticket_holder_name": "John Doe",
            },
        )

        title = TicketCreatedTemplate().get_in_app_title(notification)

        assert "John Doe" not in title
        assert "Ticket Event" in title

    def test_staff_ticket_templates_title_names_holder(self, ticket_holder: RevelUser) -> None:
        """Each staff ticket type has its own template that names the holder and the event."""
        for notification_type, expected in (
            (NotificationType.STAFF_TICKET_CREATED, "New Ticket"),
            (NotificationType.STAFF_TICKET_CANCELLED, "Ticket Cancelled"),
            (NotificationType.STAFF_TICKET_REFUNDED, "Ticket Refunded"),
        ):
            notification = _create_notification_for_test(
                user=ticket_holder,
                notification_type=notification_type,
                context={"event_name": "Ticket Event", "ticket_holder_name": "John Doe"},
            )
            template = get_template(notification_type)

            for title in (template.get_in_app_title(notification), template.get_email_subject(notification)):
                assert expected in title
                assert "John Doe" in title
                assert "Ticket Event" in title

    def test_staff_payment_disputed_titles(self, ticket_holder: RevelUser) -> None:
        """The dispute template distinguishes an opened dispute (amount) from a closed one (outcome)."""
        template = get_template(NotificationType.STAFF_PAYMENT_DISPUTED)
        opened = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.STAFF_PAYMENT_DISPUTED,
            context={"outcome": "opened", "amount": "50.00", "currency": "EUR"},
        )
        closed = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.STAFF_PAYMENT_DISPUTED,
            context={"outcome": "closed", "status": "lost"},
        )

        assert "50.00 EUR" in template.get_in_app_title(opened)
        assert "Lost" in template.get_email_subject(closed)

    def test_staff_payment_disputed_labels_follow_each_recipients_language(
        self, django_user_model: t.Type[RevelUser]
    ) -> None:
        """Status and reason labels are resolved per recipient, in every channel (compiled catalogs needed)."""
        template = get_template(NotificationType.STAFF_PAYMENT_DISPUTED)
        context: dict[str, object] = {
            "outcome": "closed",
            "status": "lost",
            "reason": "fraudulent",
            "amount": "50.00",
            "currency": "EUR",
        }
        expected = {"de": ("Verloren", "Betrügerisch"), "it": ("Persa", "Fraudolento")}
        rendered: dict[str, list[str]] = {}
        for language in expected:
            user = django_user_model.objects.create_user(
                username=f"staff-{language}@example.com",
                email=f"staff-{language}@example.com",
                password="pw",
                language=language,
            )
            notification = _create_notification_for_test(
                user=user, notification_type=NotificationType.STAFF_PAYMENT_DISPUTED, context=context
            )
            with translation.override(language):
                rendered[language] = [
                    template.get_in_app_title(notification),
                    template.get_in_app_body(notification),
                    template.get_email_text_body(notification),
                    template.get_telegram_body(notification),
                ]

        for language, (status, reason) in expected.items():
            assert status in rendered[language][0]
            for body in rendered[language][1:]:
                assert status in body
                assert reason in body
            other_status, other_reason = expected["it" if language == "de" else "de"]
            assert not any(other_status in text or other_reason in text for text in rendered[language])

    @patch("notifications.service.templates.ticket_templates._build_ticket_attachments")
    def test_get_email_attachments_calls_build_function(
        self,
        mock_build: MagicMock,
        ticket_holder: RevelUser,
        active_ticket: Ticket,
    ) -> None:
        """Should call _build_ticket_attachments with correct params."""
        mock_build.return_value = {}
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.TICKET_CREATED,
            context={
                "ticket_id": str(active_ticket.id),
                "event_id": str(active_ticket.event.id),
                "event_name": "Ticket Event",
                "include_pdf": True,
                "include_ics": True,
                "include_pkpass": True,
            },
        )
        template = TicketCreatedTemplate()

        template.get_email_attachments(notification)

        mock_build.assert_called_once()
        call_kwargs = mock_build.call_args[1]
        assert call_kwargs["include_pdf"] is True
        assert call_kwargs["include_ics"] is True
        assert call_kwargs["include_pkpass"] is True

    def test_pending_email_explains_ticket_arrives_after_payment(
        self,
        ticket_holder: RevelUser,
        active_ticket: Ticket,
    ) -> None:
        """Pending emails carry no ticket files, so they must set expectations."""
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.TICKET_CREATED,
            context={
                "event_name": active_ticket.event.name,
                "ticket_id": str(active_ticket.id),
                "event_id": str(active_ticket.event.id),
                "tier_name": active_ticket.tier.name,
                "ticket_status": "pending",
            },
        )
        template = TicketCreatedTemplate()

        html = template.get_email_html_body(notification)
        text = template.get_email_text_body(notification)

        expected = "Your ticket will arrive by email as soon as your payment is confirmed."
        assert html is not None and expected in html
        assert expected in text


class TestTicketUpdatedTemplate:
    """Tests for TicketUpdatedTemplate."""

    def test_get_in_app_title_for_activation(
        self,
        ticket_holder: RevelUser,
    ) -> None:
        """Should return confirmed title when ticket is activated."""
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.TICKET_UPDATED,
            context={
                "event_name": "Ticket Event",
                "old_status": "pending",
                "new_status": "active",
            },
        )
        template = TicketUpdatedTemplate()

        title = template.get_in_app_title(notification)

        assert "Confirmed" in title

    def test_get_in_app_title_for_general_update(
        self,
        ticket_holder: RevelUser,
    ) -> None:
        """Should return update title for general updates."""
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.TICKET_UPDATED,
            context={
                "event_name": "Ticket Event",
                "action": "updated",
            },
        )
        template = TicketUpdatedTemplate()

        title = template.get_in_app_title(notification)

        assert "Update" in title

    def test_activation_email_text_renders_tier_name(
        self,
        ticket_holder: RevelUser,
        active_ticket: Ticket,
    ) -> None:
        """Regression: a malformed tag rendered '{{ context.tier_name %}' literally."""
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.TICKET_UPDATED,
            context={
                "event_name": active_ticket.event.name,
                "ticket_id": str(active_ticket.id),
                "event_id": str(active_ticket.event.id),
                "tier_name": active_ticket.tier.name,
                "old_status": "pending",
                "new_status": "active",
                "action": "activated",
            },
        )
        template = TicketUpdatedTemplate()

        text = template.get_email_text_body(notification)

        assert active_ticket.tier.name in text
        assert "{{ context.tier_name" not in text

    @patch("notifications.service.templates.ticket_templates._build_ticket_attachments")
    def test_get_email_attachments_no_attachments_for_cancellation(
        self,
        mock_build: MagicMock,
        ticket_holder: RevelUser,
    ) -> None:
        """Should return empty dict for cancellations."""
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.TICKET_UPDATED,
            context={
                "event_name": "Ticket Event",
                "include_pdf": False,
                "include_ics": False,
                "include_pkpass": False,
            },
        )
        template = TicketUpdatedTemplate()

        result = template.get_email_attachments(notification)

        assert result == {}
        mock_build.assert_not_called()


class TestPaymentConfirmationTemplate:
    """Tests for PaymentConfirmationTemplate."""

    def test_get_in_app_title(
        self,
        ticket_holder: RevelUser,
    ) -> None:
        """Should return payment confirmation title."""
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.PAYMENT_CONFIRMATION,
            context={"event_name": "Test Event"},  # Minimal context
        )
        template = PaymentConfirmationTemplate()

        title = template.get_in_app_title(notification)

        assert "Payment Confirmation" in title

    @patch("notifications.service.templates.ticket_templates._build_ticket_attachments")
    def test_get_email_attachments_includes_all(
        self,
        mock_build: MagicMock,
        ticket_holder: RevelUser,
        active_ticket: Ticket,
    ) -> None:
        """Should include all attachments for payment confirmation."""
        mock_build.return_value = {}
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.PAYMENT_CONFIRMATION,
            context={
                "ticket_id": str(active_ticket.id),
                "event_id": str(active_ticket.event.id),
                "event_name": active_ticket.event.name,
            },
        )
        template = PaymentConfirmationTemplate()

        template.get_email_attachments(notification)

        mock_build.assert_called_once()
        call_kwargs = mock_build.call_args[1]
        assert call_kwargs["include_pdf"] is True
        assert call_kwargs["include_ics"] is True
        assert call_kwargs["include_pkpass"] is True


class TestTicketCheckedInTemplate:
    """Tests for TicketCheckedInTemplate."""

    def test_get_in_app_title(
        self,
        ticket_holder: RevelUser,
        active_ticket: Ticket,
    ) -> None:
        """Should return checked in title with event name."""
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.TICKET_CHECKED_IN,
            context={
                "event_name": active_ticket.event.name,
            },
        )
        template = TicketCheckedInTemplate()

        title = template.get_in_app_title(notification)

        assert "Checked in" in title
        assert active_ticket.event.name in title

    def test_get_email_subject(
        self,
        ticket_holder: RevelUser,
        active_ticket: Ticket,
    ) -> None:
        """Should return checked in subject with event name."""
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.TICKET_CHECKED_IN,
            context={
                "event_name": active_ticket.event.name,
            },
        )
        template = TicketCheckedInTemplate()

        subject = template.get_email_subject(notification)

        assert "Checked in" in subject
        assert active_ticket.event.name in subject


_CANCELLATION_CHANNELS: tuple[str, ...] = (
    "notifications/in_app/ticket_cancelled.md",
    "notifications/email/ticket_cancelled.html",
    "notifications/email/ticket_cancelled.txt",
    "notifications/telegram/ticket_cancelled.md",
)


class TestTicketCancelledTemplateBranching:
    """Tests that ticket_cancelled templates branch the headline on cancellation_source."""

    def _render(
        self,
        template: str,
        *,
        source: str,
        event_name: str = "Test Fest",
    ) -> str:
        """Render a ticket_cancelled template with the given cancellation_source."""
        from django.template.loader import render_to_string

        return render_to_string(
            template,
            {
                "user": {"display_name": "Test User"},
                "context": {
                    "event_name": event_name,
                    "event_start_formatted": "TBD",
                    "event_location": "",
                    "tier_name": "GA",
                    "ticket_id": "abc",
                    "event_url": "https://example.com",
                    "cancellation_source": source,
                    "cancellation_reason": "",
                },
            },
        )

    @pytest.mark.parametrize("template", _CANCELLATION_CHANNELS)
    def test_user_source_uses_self_cancellation_headline(self, template: str) -> None:
        """When cancellation_source is USER, headline says 'You cancelled' in every channel."""
        rendered = self._render(template, source=CancellationSource.USER.value)
        assert "You cancelled your ticket" in rendered

    @pytest.mark.parametrize("template", _CANCELLATION_CHANNELS)
    def test_stripe_dashboard_source_mentions_refund(self, template: str) -> None:
        """When cancellation_source is STRIPE_DASHBOARD, headline mentions refund in every channel."""
        rendered = self._render(template, source=CancellationSource.STRIPE_DASHBOARD.value)
        assert "cancelled and refunded" in rendered

    @pytest.mark.parametrize("template", _CANCELLATION_CHANNELS)
    def test_organizer_source_uses_passive_headline(self, template: str) -> None:
        """When cancellation_source is ORGANIZER, every channel uses passive voice."""
        rendered = self._render(template, source=CancellationSource.ORGANIZER.value)
        assert "has been cancelled" in rendered
        assert "You cancelled" not in rendered
        assert "refunded" not in rendered

    @pytest.mark.parametrize("template", _CANCELLATION_CHANNELS)
    def test_empty_source_uses_passive_headline(self, template: str) -> None:
        """Backward-compat: missing/empty cancellation_source falls through to the default copy."""
        rendered = self._render(template, source="")
        assert "has been cancelled" in rendered
        assert "You cancelled" not in rendered


_TICKET_CREATED_CHANNELS: tuple[str, ...] = (
    "notifications/in_app/ticket_created.md",
    "notifications/email/ticket_created.html",
    "notifications/email/ticket_created.txt",
    "notifications/telegram/ticket_created.md",
)


class TestPendingTicketPaymentInstructions:
    """Tests that organizer payment instructions don't duplicate the template's own heading."""

    def _render(self, template: str, *, instructions: str | None) -> str:
        """Render a pending-ticket notification with the given manual payment instructions."""
        from django.template.loader import render_to_string

        return render_to_string(
            template,
            {
                "user": {"display_name": "Test User"},
                "context": {
                    "event_name": "Test Fest",
                    "event_start_formatted": "TBD",
                    "event_location": "",
                    "tier_name": "GA",
                    "ticket_id": "abc",
                    "ticket_status": "pending",
                    "event_url": "https://example.com",
                    "manual_payment_instructions": instructions,
                },
            },
        )

    @pytest.mark.parametrize("template", _TICKET_CREATED_CHANNELS)
    def test_leading_heading_in_instructions_is_not_duplicated(self, template: str) -> None:
        """The template supplies the heading, so the organizer's leading one is dropped."""
        rendered = self._render(template, instructions="## Payment Instructions\n\nTransfer to IBAN AT12.")

        assert rendered.count("Payment Instructions") == 1
        assert "Transfer to IBAN AT12." in rendered

    @pytest.mark.parametrize("template", _TICKET_CREATED_CHANNELS)
    def test_instructions_without_heading_are_preserved(self, template: str) -> None:
        """Bare instructions still render under the template's heading."""
        rendered = self._render(template, instructions="Please transfer to IBAN: XX1234567890")

        assert "Payment Instructions" in rendered
        assert "Please transfer to IBAN: XX1234567890" in rendered

    @pytest.mark.parametrize("template", _TICKET_CREATED_CHANNELS)
    def test_single_line_heading_instructions_are_not_swallowed(self, template: str) -> None:
        """Instructions written as one heading line must still reach the recipient.

        The templates gate on the raw value, so stripping this to nothing would render
        a Payment Instructions label with an empty body and no fallback copy.
        """
        rendered = self._render(template, instructions="# Please e-transfer to IBAN AT12 3456 7890")

        assert "Please e-transfer to IBAN AT12 3456 7890" in rendered

    @pytest.mark.parametrize("template", _TICKET_CREATED_CHANNELS)
    def test_missing_instructions_fall_back_to_contact_copy(self, template: str) -> None:
        """Without instructions every channel shows the fallback copy."""
        rendered = self._render(template, instructions=None)

        assert "contact the organizer" in rendered
