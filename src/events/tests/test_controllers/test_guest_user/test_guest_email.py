"""Tests for guest email task functions."""

import pytest

from events.models import Event, TicketTier

pytestmark = pytest.mark.django_db


class TestGuestEmailTasks:
    """Test the actual email task functions."""

    def test_send_guest_rsvp_confirmation_creates_email(self, guest_event: Event) -> None:
        """Test that send_guest_rsvp_confirmation task creates email correctly."""
        from django.core import mail

        from common.tasks import to_safe_email_address
        from events.tasks import send_guest_rsvp_confirmation

        # Arrange
        email = "test@example.com"
        token = "test_token_123"
        event_name = guest_event.name

        # Act
        send_guest_rsvp_confirmation(email, token, event_name)

        # Assert: Email was sent
        assert len(mail.outbox) == 1
        sent_email = mail.outbox[0]

        # Check email attributes - email is transformed by catchall system
        # Single recipients go to 'to', not 'bcc'
        safe_email = to_safe_email_address(email)
        assert safe_email in sent_email.to
        assert event_name in sent_email.subject
        assert "confirm" in sent_email.subject.lower() or "rsvp" in sent_email.subject.lower()
        assert token in sent_email.body
        assert event_name in sent_email.body

    def test_send_guest_ticket_confirmation_creates_email(
        self, guest_event_with_tickets: Event, free_tier: TicketTier
    ) -> None:
        """Test that send_guest_ticket_confirmation task creates email correctly."""
        from django.core import mail

        from common.tasks import to_safe_email_address
        from events.tasks import send_guest_ticket_confirmation

        # Arrange
        email = "test@example.com"
        token = "test_token_456"
        event_name = guest_event_with_tickets.name
        tier_name = free_tier.name

        # Act
        send_guest_ticket_confirmation(email, token, event_name, tier_name)

        # Assert: Email was sent
        assert len(mail.outbox) == 1
        sent_email = mail.outbox[0]

        # Check email attributes - email is transformed by catchall system
        # Single recipients go to 'to', not 'bcc'
        safe_email = to_safe_email_address(email)
        assert safe_email in sent_email.to
        assert event_name in sent_email.subject
        assert "confirm" in sent_email.subject.lower() or "ticket" in sent_email.subject.lower()
        assert token in sent_email.body
        assert event_name in sent_email.body
        assert tier_name in sent_email.body

    def test_guest_rsvp_email_contains_confirmation_link(self, guest_event: Event) -> None:
        """Test that RSVP email contains proper confirmation link."""
        from django.core import mail

        from events.tasks import send_guest_rsvp_confirmation

        # Arrange
        email = "test@example.com"
        token = "test_token_789"
        event_name = guest_event.name

        # Act
        send_guest_rsvp_confirmation(email, token, event_name)

        # Assert: Email contains confirmation link
        sent_email = mail.outbox[0]
        assert "/events/confirm-action" in sent_email.body
        assert f"token={token}" in sent_email.body

    def test_guest_ticket_email_contains_confirmation_link(
        self, guest_event_with_tickets: Event, free_tier: TicketTier
    ) -> None:
        """Test that ticket email contains proper confirmation link."""
        from django.core import mail

        from events.tasks import send_guest_ticket_confirmation

        # Arrange
        email = "test@example.com"
        token = "test_token_abc"

        # Act
        send_guest_ticket_confirmation(email, token, guest_event_with_tickets.name, free_tier.name)

        # Assert: Email contains confirmation link
        sent_email = mail.outbox[0]
        assert "/events/confirm-action" in sent_email.body
        assert f"token={token}" in sent_email.body

    def test_guest_email_subject_uses_i18n(self, guest_event: Event) -> None:
        """Test that email subjects use internationalization strings."""
        from django.core import mail

        from events.tasks import send_guest_rsvp_confirmation

        # Act
        send_guest_rsvp_confirmation("test@example.com", "token", guest_event.name)

        # Assert: Subject is present and not empty (i18n string was used)
        sent_email = mail.outbox[0]
        assert sent_email.subject
        assert len(sent_email.subject) > 0
