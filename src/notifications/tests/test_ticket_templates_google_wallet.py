"""Tests for the Google Wallet save link in ticket emails."""

import typing as t
from pathlib import Path

import pytest

from accounts.models import RevelUser
from events.models import Ticket
from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.ticket_templates import (
    PaymentConfirmationTemplate,
    TicketCreatedTemplate,
    TicketUpdatedTemplate,
)
from notifications.tests.conftest import _create_notification_for_test

pytestmark = pytest.mark.django_db


def _make_notification(ticket_holder: RevelUser, active_ticket: Ticket, **extra: object) -> Notification:
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


@pytest.fixture
def apple_wallet_settings(settings: t.Any) -> None:
    """Configure Apple Wallet settings (values need not point at real files)."""
    settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.example.test"
    settings.APPLE_WALLET_TEAM_ID = "TEAM123"
    settings.APPLE_WALLET_CERT_PATH = "/path/cert.pem"
    settings.APPLE_WALLET_KEY_PATH = "/path/key.pem"
    settings.APPLE_WALLET_WWDR_CERT_PATH = "/path/wwdr.pem"


class TestGoogleWalletEmailLink:
    """Tests for the Google Wallet save link in ticket emails."""

    def test_link_present_when_configured(
        self, ticket_holder: RevelUser, active_ticket: Ticket, google_wallet_settings: None
    ) -> None:
        notification = _make_notification(ticket_holder, active_ticket)
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
        notification = _make_notification(ticket_holder, active_ticket)
        template = TicketCreatedTemplate()

        html = template.get_email_html_body(notification)

        assert html is not None and "pay.google.com" not in html

    def test_link_absent_when_include_pkpass_false(
        self, ticket_holder: RevelUser, active_ticket: Ticket, google_wallet_settings: None
    ) -> None:
        notification = _make_notification(ticket_holder, active_ticket, include_pkpass=False)
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


class TestAppleWalletEmailLink:
    """Tests for the signed Apple Wallet link in ticket email context."""

    def test_apple_url_present_and_verifiable(
        self,
        ticket_holder: RevelUser,
        active_ticket: Ticket,
        apple_wallet_settings: None,
        google_wallet_settings: None,
    ) -> None:
        from urllib.parse import parse_qs, urlparse

        from common.signing import verify_signature

        notification = _make_notification(ticket_holder, active_ticket)
        template = TicketCreatedTemplate()

        # NOTE: asserted at the context level (not rendered HTML) — Task 4 wires the
        # Apple badge markup into the email templates; until then the HTML doesn't
        # surface this URL even though the context already carries it.
        ctx = template._get_template_context(notification)
        assert "apple_wallet_signed_url" in ctx["context"]
        assert "google_wallet_save_url" in ctx["context"]

        url = ctx["context"]["apple_wallet_signed_url"]
        assert "/wallet/apple/signed" in url
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        assert verify_signature(parsed.path, params["exp"][0], params["sig"][0])

    def test_apple_url_absent_when_unconfigured(
        self,
        ticket_holder: RevelUser,
        active_ticket: Ticket,
        google_wallet_settings: None,
        settings: t.Any,
    ) -> None:
        settings.APPLE_WALLET_PASS_TYPE_ID = ""
        settings.APPLE_WALLET_TEAM_ID = ""
        settings.APPLE_WALLET_CERT_PATH = ""
        settings.APPLE_WALLET_KEY_PATH = ""
        settings.APPLE_WALLET_WWDR_CERT_PATH = ""
        notification = _make_notification(ticket_holder, active_ticket)

        html = TicketCreatedTemplate().get_email_html_body(notification)

        assert html is not None and "/wallet/apple/signed" not in html

    def test_apple_url_absent_for_past_event(
        self,
        ticket_holder: RevelUser,
        active_ticket: Ticket,
        apple_wallet_settings: None,
        google_wallet_settings: None,
    ) -> None:
        from datetime import timedelta

        from django.utils import timezone

        # More than SIGNED_LINK_GRACE_PERIOD (1 week) past event end
        active_ticket.event.start = timezone.now() - timedelta(days=10)
        active_ticket.event.end = timezone.now() - timedelta(days=9)
        active_ticket.event.save(update_fields=["start", "end"])
        notification = _make_notification(ticket_holder, active_ticket)

        html = TicketCreatedTemplate().get_email_html_body(notification)

        assert html is not None and "/wallet/apple/signed" not in html

    def test_payment_confirmation_gets_badges(
        self,
        ticket_holder: RevelUser,
        active_ticket: Ticket,
        apple_wallet_settings: None,
        google_wallet_settings: None,
    ) -> None:
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.PAYMENT_CONFIRMATION,
            context={
                "event_name": active_ticket.event.name,
                "ticket_id": str(active_ticket.id),
                "event_id": str(active_ticket.event.id),
                "tier_name": active_ticket.tier.name,
                "payment_amount": "10.00",
                "payment_currency": "EUR",
                "payment_id": "pay_123",
                "payment_date": "2026-08-10",
            },
        )

        # NOTE: context-level assertion — see test_apple_url_present_and_verifiable above.
        ctx = PaymentConfirmationTemplate()._get_template_context(notification)
        assert "apple_wallet_signed_url" in ctx["context"]
        assert "google_wallet_save_url" in ctx["context"]
