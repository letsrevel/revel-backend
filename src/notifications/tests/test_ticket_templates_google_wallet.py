"""Tests for the Google Wallet save link in ticket emails."""

import typing as t
from pathlib import Path

import pytest

from accounts.models import RevelUser
from events.models import Ticket
from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.ticket_templates import TicketCreatedTemplate, TicketUpdatedTemplate
from notifications.tests.conftest import _create_notification_for_test

pytestmark = pytest.mark.django_db


@pytest.fixture
def google_wallet_settings(settings: t.Any, tmp_path: Path) -> None:
    """Configure Google Wallet with a real (test-only) service-account key."""
    import json

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    sa_path = tmp_path / "sa.json"
    sa_path.write_text(json.dumps({"client_email": "wallet@test.iam.gserviceaccount.com", "private_key": pem}))
    settings.GOOGLE_WALLET_ISSUER_ID = "3388000000012345678"
    settings.GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH = str(sa_path)
    settings.GOOGLE_WALLET_CLASS_PREFIX = "test"


class TestGoogleWalletEmailLink:
    """Tests for the Google Wallet save link in ticket emails."""

    @staticmethod
    def _notification(ticket_holder: RevelUser, active_ticket: Ticket, **extra: object) -> Notification:
        return _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.TICKET_CREATED,
            context={
                "event_name": active_ticket.event.name,
                "ticket_status": "active",
                "ticket_id": str(active_ticket.id),
                "event_id": str(active_ticket.event.id),
                **extra,
            },
        )

    def test_link_present_when_configured(
        self, ticket_holder: RevelUser, active_ticket: Ticket, google_wallet_settings: None
    ) -> None:
        notification = self._notification(ticket_holder, active_ticket)
        template = TicketCreatedTemplate()

        html = template.get_email_html_body(notification)
        text = template.get_email_text_body(notification)

        assert html is not None and "https://pay.google.com/gp/v/save/" in html
        assert "https://pay.google.com/gp/v/save/" in text

    def test_link_absent_when_unconfigured(
        self, ticket_holder: RevelUser, active_ticket: Ticket, settings: t.Any
    ) -> None:
        settings.GOOGLE_WALLET_ISSUER_ID = ""
        settings.GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH = ""
        notification = self._notification(ticket_holder, active_ticket)
        template = TicketCreatedTemplate()

        html = template.get_email_html_body(notification)

        assert html is not None and "pay.google.com" not in html

    def test_link_absent_when_include_pkpass_false(
        self, ticket_holder: RevelUser, active_ticket: Ticket, google_wallet_settings: None
    ) -> None:
        notification = self._notification(ticket_holder, active_ticket, include_pkpass=False)
        template = TicketCreatedTemplate()

        html = template.get_email_html_body(notification)

        assert html is not None and "pay.google.com" not in html

    def test_link_absent_when_ticket_cancelled(
        self, ticket_holder: RevelUser, active_ticket: Ticket, google_wallet_settings: None
    ) -> None:
        """TICKET_CANCELLED/TICKET_REFUNDED reuse TicketUpdatedTemplate with include_pkpass

        defaulting True — a cancelled ticket must never get a save link, mirroring
        TicketWalletController.get_queryset()'s ACTIVE/PENDING status filter.
        """
        active_ticket.status = Ticket.TicketStatus.CANCELLED
        active_ticket.save(update_fields=["status"])
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.TICKET_UPDATED,
            context={
                "event_name": active_ticket.event.name,
                "action": "cancelled",
                "ticket_status": "cancelled",
                "ticket_id": str(active_ticket.id),
                "event_id": str(active_ticket.event.id),
            },
        )
        template = TicketUpdatedTemplate()

        html = template.get_email_html_body(notification)

        assert html is not None and "pay.google.com" not in html

    def test_ticket_updated_activation_link_present_when_configured(
        self, ticket_holder: RevelUser, active_ticket: Ticket, google_wallet_settings: None
    ) -> None:
        """Pins the TicketUpdatedTemplate placement in the pending->active activation branch."""
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.TICKET_UPDATED,
            context={
                "event_name": active_ticket.event.name,
                "old_status": "pending",
                "new_status": "active",
                "ticket_status": "active",
                "ticket_id": str(active_ticket.id),
                "event_id": str(active_ticket.event.id),
            },
        )
        template = TicketUpdatedTemplate()

        html = template.get_email_html_body(notification)
        text = template.get_email_text_body(notification)

        assert html is not None and "https://pay.google.com/gp/v/save/" in html
        assert "https://pay.google.com/gp/v/save/" in text
