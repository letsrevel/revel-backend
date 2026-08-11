"""Tests for wallet badges in membership emails and the MEMBERSHIP_CARD_UPDATED notification.

Covers:
- ``_add_membership_wallet_context`` gating (config + member visibility status), wired
  into ``MembershipGrantedTemplate``.
- ``events.signals.handle_membership_tier_changed`` emitting MEMBERSHIP_CARD_UPDATED
  exactly once on a tier change, and not on creation or a status-only change.
"""

import typing as t
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest

from accounts.models import RevelUser
from common.signing import verify_signature
from events.models import MembershipTier, Organization, OrganizationMember
from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.membership_templates import MembershipGrantedTemplate
from notifications.tests.conftest import _create_notification_for_test

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier_a(organization: Organization) -> MembershipTier:
    """First membership tier for tier-change tests."""
    return MembershipTier.objects.create(organization=organization, name="Silver")


@pytest.fixture
def tier_b(organization: Organization) -> MembershipTier:
    """Second membership tier for tier-change tests."""
    return MembershipTier.objects.create(organization=organization, name="Gold")


@pytest.fixture
def member(organization: Organization, member_user: RevelUser, tier_a: MembershipTier) -> OrganizationMember:
    """An ACTIVE organization member on tier_a."""
    return OrganizationMember.objects.create(organization=organization, user=member_user, tier=tier_a)


@pytest.fixture
def apple_wallet_settings(settings: t.Any) -> None:
    """Configure Apple Wallet settings (values need not point at real files)."""
    settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.example.test"
    settings.APPLE_WALLET_TEAM_ID = "TEAM123"
    settings.APPLE_WALLET_CERT_PATH = "/path/cert.pem"
    settings.APPLE_WALLET_KEY_PATH = "/path/key.pem"
    settings.APPLE_WALLET_WWDR_CERT_PATH = "/path/wwdr.pem"


@pytest.fixture
def google_wallet_settings(settings: t.Any) -> None:
    """Configure Google Wallet settings (values need not point at a real key file)."""
    settings.GOOGLE_WALLET_ISSUER_ID = "3388000000012345678"
    settings.GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH = "/path/sa.json"


@pytest.fixture
def neither_wallet_configured(settings: t.Any) -> None:
    """Explicitly clear both rails — local dev .env may configure real certs/keys,

    which would otherwise leak into this "unconfigured" case and falsely pass or
    fail depending on machine state (mirrors test_ticket_templates_google_wallet.py).
    """
    settings.APPLE_WALLET_PASS_TYPE_ID = ""
    settings.APPLE_WALLET_TEAM_ID = ""
    settings.APPLE_WALLET_CERT_PATH = ""
    settings.APPLE_WALLET_KEY_PATH = ""
    settings.APPLE_WALLET_WWDR_CERT_PATH = ""
    settings.GOOGLE_WALLET_ISSUER_ID = ""
    settings.GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH = ""


def _granted_notification(member_user: RevelUser, organization: Organization) -> Notification:
    return _create_notification_for_test(
        user=member_user,
        notification_type=NotificationType.MEMBERSHIP_GRANTED,
        context={
            "organization_id": str(organization.id),
            "organization_name": organization.name,
            "role": "member",
            "action": "granted",
            "frontend_url": "https://example.com/org/test-org",
        },
    )


class TestMembershipWalletBadges:
    """Tests for _add_membership_wallet_context via MembershipGrantedTemplate."""

    def test_google_badge_present_when_configured(
        self,
        member_user: RevelUser,
        organization: Organization,
        member: OrganizationMember,
        google_wallet_settings: None,
    ) -> None:
        notification = _granted_notification(member_user, organization)

        with patch(
            "wallet.google.service.membership_save_url",
            return_value="https://pay.google.com/gp/v/save/xyz",
        ) as mock_save_url:
            ctx = MembershipGrantedTemplate()._get_template_context(notification)

        assert ctx["context"]["google_wallet_save_url"] == "https://pay.google.com/gp/v/save/xyz"
        mock_save_url.assert_called_once_with(member)

    def test_apple_badge_present_and_verifiable_when_configured(
        self,
        member_user: RevelUser,
        organization: Organization,
        member: OrganizationMember,
        apple_wallet_settings: None,
    ) -> None:
        notification = _granted_notification(member_user, organization)

        ctx = MembershipGrantedTemplate()._get_template_context(notification)

        url = ctx["context"]["apple_wallet_signed_url"]
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        assert "exp" in params
        assert "sig" in params
        assert verify_signature(parsed.path, params["exp"][0], params["sig"][0])

    def test_no_badges_when_neither_rail_configured(
        self,
        member_user: RevelUser,
        organization: Organization,
        member: OrganizationMember,
        neither_wallet_configured: None,
    ) -> None:
        notification = _granted_notification(member_user, organization)

        ctx = MembershipGrantedTemplate()._get_template_context(notification)

        assert "apple_wallet_signed_url" not in ctx["context"]
        assert "google_wallet_save_url" not in ctx["context"]

    def test_no_badges_when_member_cancelled(
        self,
        member_user: RevelUser,
        organization: Organization,
        member: OrganizationMember,
        apple_wallet_settings: None,
        google_wallet_settings: None,
    ) -> None:
        member.status = OrganizationMember.MembershipStatus.CANCELLED
        member.save(update_fields=["status"])
        notification = _granted_notification(member_user, organization)

        with patch(
            "wallet.google.service.membership_save_url",
            return_value="https://pay.google.com/gp/v/save/xyz",
        ):
            ctx = MembershipGrantedTemplate()._get_template_context(notification)

        assert "apple_wallet_signed_url" not in ctx["context"]
        assert "google_wallet_save_url" not in ctx["context"]


class TestMembershipTierChangedSignal:
    """Tests for events.signals.handle_membership_tier_changed."""

    def test_tier_change_emits_card_updated_once(
        self,
        member: OrganizationMember,
        tier_b: MembershipTier,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        with patch("events.signals.notification_requested.send") as mock_send:
            with django_capture_on_commit_callbacks(execute=True):
                member.tier = tier_b
                member.save(update_fields=["tier"])

        card_updates = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("notification_type") == NotificationType.MEMBERSHIP_CARD_UPDATED
        ]
        assert len(card_updates) == 1
        context = card_updates[0].kwargs["context"]
        assert context["organization_id"] == str(member.organization_id)
        assert context["organization_name"] == member.organization.name
        assert context["tier_name"] == tier_b.name

    def test_member_creation_does_not_emit_card_updated(
        self,
        organization: Organization,
        member_user: RevelUser,
        tier_a: MembershipTier,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        with patch("events.signals.notification_requested.send") as mock_send:
            with django_capture_on_commit_callbacks(execute=True):
                OrganizationMember.objects.create(organization=organization, user=member_user, tier=tier_a)

        card_updates = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("notification_type") == NotificationType.MEMBERSHIP_CARD_UPDATED
        ]
        assert card_updates == []

    def test_status_only_change_does_not_emit_card_updated(
        self,
        member: OrganizationMember,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        with patch("events.signals.notification_requested.send") as mock_send:
            with django_capture_on_commit_callbacks(execute=True):
                member.status = OrganizationMember.MembershipStatus.PAUSED
                member.save(update_fields=["status"])

        card_updates = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("notification_type") == NotificationType.MEMBERSHIP_CARD_UPDATED
        ]
        assert card_updates == []
