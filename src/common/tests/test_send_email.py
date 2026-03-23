"""Tests for common.tasks.send_email with from_email and reply_to parameters.

Covers:
- ``from_email`` is passed through to ``EmailMultiAlternatives``.
- ``reply_to`` is passed through to ``EmailMultiAlternatives``.
- Defaults to ``settings.DEFAULT_FROM_EMAIL`` when ``from_email`` is not specified.
- Defaults to empty reply_to when not specified.
- Both params work together for billing emails.
"""

from unittest.mock import MagicMock, patch

import pytest
from django.conf import settings

from common.models import SiteSettings
from common.tasks import send_email

pytestmark = pytest.mark.django_db


@pytest.fixture
def site_settings() -> SiteSettings:
    """Ensure SiteSettings singleton exists with live emails enabled.

    Live emails are enabled so the safe-email logic doesn't transform addresses.
    """
    site = SiteSettings.get_solo()
    site.live_emails = True
    site.save()
    return site


class TestSendEmailFromEmail:
    """Test the from_email parameter of send_email."""

    @patch("common.tasks.EmailMultiAlternatives")
    def test_from_email_passed_through_to_email_message(
        self,
        mock_email_cls: MagicMock,
        site_settings: SiteSettings,
    ) -> None:
        """When from_email is provided, it is used as the sender address."""
        mock_instance = mock_email_cls.return_value
        mock_instance.send.return_value = 1

        send_email(
            to="recipient@example.com",
            subject="Test",
            body="Body",
            html_body="<p>Body</p>",
            from_email="billing@revel.at",
        )

        call_kwargs = mock_email_cls.call_args.kwargs
        assert call_kwargs["from_email"] == "billing@revel.at"

    @patch("common.tasks.EmailMultiAlternatives")
    def test_defaults_to_settings_default_from_email(
        self,
        mock_email_cls: MagicMock,
        site_settings: SiteSettings,
    ) -> None:
        """When from_email is not specified, settings.DEFAULT_FROM_EMAIL is used."""
        mock_instance = mock_email_cls.return_value
        mock_instance.send.return_value = 1

        send_email(
            to="recipient@example.com",
            subject="Test",
            body="Body",
            html_body="<p>Body</p>",
        )

        call_kwargs = mock_email_cls.call_args.kwargs
        assert call_kwargs["from_email"] == settings.DEFAULT_FROM_EMAIL

    @patch("common.tasks.EmailMultiAlternatives")
    def test_none_from_email_falls_back_to_default(
        self,
        mock_email_cls: MagicMock,
        site_settings: SiteSettings,
    ) -> None:
        """Explicitly passing from_email=None falls back to DEFAULT_FROM_EMAIL."""
        mock_instance = mock_email_cls.return_value
        mock_instance.send.return_value = 1

        send_email(
            to="recipient@example.com",
            subject="Test",
            body="Body",
            html_body="<p>Body</p>",
            from_email=None,
        )

        call_kwargs = mock_email_cls.call_args.kwargs
        assert call_kwargs["from_email"] == settings.DEFAULT_FROM_EMAIL


class TestSendEmailReplyTo:
    """Test the reply_to parameter of send_email."""

    @patch("common.tasks.EmailMultiAlternatives")
    def test_reply_to_passed_through_to_email_message(
        self,
        mock_email_cls: MagicMock,
        site_settings: SiteSettings,
    ) -> None:
        """When reply_to is provided, it is set on the email message."""
        mock_instance = mock_email_cls.return_value
        mock_instance.send.return_value = 1

        send_email(
            to="recipient@example.com",
            subject="Test",
            body="Body",
            html_body="<p>Body</p>",
            reply_to=["support@revel.at"],
        )

        call_kwargs = mock_email_cls.call_args.kwargs
        assert call_kwargs["reply_to"] == ["support@revel.at"]

    @patch("common.tasks.EmailMultiAlternatives")
    def test_default_reply_to_is_empty_list(
        self,
        mock_email_cls: MagicMock,
        site_settings: SiteSettings,
    ) -> None:
        """When reply_to is not specified, an empty list is used."""
        mock_instance = mock_email_cls.return_value
        mock_instance.send.return_value = 1

        send_email(
            to="recipient@example.com",
            subject="Test",
            body="Body",
            html_body="<p>Body</p>",
        )

        call_kwargs = mock_email_cls.call_args.kwargs
        assert call_kwargs["reply_to"] == []

    @patch("common.tasks.EmailMultiAlternatives")
    def test_multiple_reply_to_addresses(
        self,
        mock_email_cls: MagicMock,
        site_settings: SiteSettings,
    ) -> None:
        """Multiple reply_to addresses are passed through."""
        mock_instance = mock_email_cls.return_value
        mock_instance.send.return_value = 1

        send_email(
            to="recipient@example.com",
            subject="Test",
            body="Body",
            html_body="<p>Body</p>",
            reply_to=["billing@revel.at", "support@revel.at"],
        )

        call_kwargs = mock_email_cls.call_args.kwargs
        assert call_kwargs["reply_to"] == ["billing@revel.at", "support@revel.at"]


class TestSendEmailFromEmailAndReplyToCombined:
    """Test from_email and reply_to used together (billing email scenario)."""

    @patch("common.tasks.EmailMultiAlternatives")
    def test_billing_email_pattern_from_and_reply_to(
        self,
        mock_email_cls: MagicMock,
        site_settings: SiteSettings,
    ) -> None:
        """The billing email pattern uses a custom from_email and reply_to together.

        This mirrors how the referral payout task dispatches emails.
        """
        mock_instance = mock_email_cls.return_value
        mock_instance.send.return_value = 1

        send_email(
            to="referrer@example.com",
            subject="Payout Statement",
            body="Attached is your statement.",
            html_body="<p>Attached is your statement.</p>",
            from_email="billing@revel.at",
            reply_to=["billing@revel.at"],
        )

        call_kwargs = mock_email_cls.call_args.kwargs
        assert call_kwargs["from_email"] == "billing@revel.at"
        assert call_kwargs["reply_to"] == ["billing@revel.at"]
        assert call_kwargs["to"] == ["referrer@example.com"]
