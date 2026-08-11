"""Tests for the member-facing membership wallet controllers in wallet/controllers.py."""

import time
import typing as t
from collections.abc import Generator
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import OrganizationMember

pytestmark = pytest.mark.django_db


@pytest.fixture
def mock_membership_pass_generator() -> Generator[MagicMock, None, None]:
    """Mock the process-cached Apple pass generator for membership controller tests."""
    mock_generator = MagicMock()
    mock_generator.generate_membership_pass.return_value = b"mock_pkpass_content"
    with patch("wallet.controllers.get_apple_pass_generator", return_value=mock_generator):
        yield mock_generator


@pytest.fixture
def mock_membership_pdf() -> Generator[MagicMock, None, None]:
    """Mock the membership PDF generator for controller tests."""
    with patch("wallet.controllers.create_membership_pdf", return_value=b"%PDF-fake") as mock_fn:
        yield mock_fn


class TestMembershipApplePass:
    """Tests for MembershipWalletController.download_apple_pass."""

    def test_returns_pkpass_file_on_success(
        self,
        apple_wallet_configured: None,
        member_client: Client,
        member: OrganizationMember,
        mock_membership_pass_generator: MagicMock,
    ) -> None:
        """Should return .pkpass file with correct content type."""
        url = reverse("api:me_membership_apple_wallet_pass", kwargs={"slug": member.organization.slug})
        response = member_client.get(url)

        assert response.status_code == 200
        assert response["Content-Type"] == "application/vnd.apple.pkpass"
        assert "Content-Disposition" in response
        assert ".pkpass" in response["Content-Disposition"]
        assert response.content == b"mock_pkpass_content"
        mock_membership_pass_generator.generate_membership_pass.assert_called_once_with(member)

    def test_returns_503_when_not_configured(
        self,
        apple_wallet_not_configured: None,
        member_client: Client,
        member: OrganizationMember,
    ) -> None:
        """Should return 503 when Apple Wallet is not configured."""
        url = reverse("api:me_membership_apple_wallet_pass", kwargs={"slug": member.organization.slug})
        response = member_client.get(url)

        assert response.status_code == 503

    def test_returns_404_for_nonmember(
        self,
        apple_wallet_configured: None,
        nonmember_client: Client,
        member: OrganizationMember,
    ) -> None:
        """Should return 404 when the caller has no membership in the org."""
        url = reverse("api:me_membership_apple_wallet_pass", kwargs={"slug": member.organization.slug})
        response = nonmember_client.get(url)

        assert response.status_code == 404

    def test_returns_401_for_unauthenticated_user(
        self,
        client: Client,
        member: OrganizationMember,
    ) -> None:
        """Should return 401 for unauthenticated requests."""
        url = reverse("api:me_membership_apple_wallet_pass", kwargs={"slug": member.organization.slug})
        response = client.get(url)

        assert response.status_code == 401

    def test_returns_404_for_cancelled_member(
        self,
        apple_wallet_configured: None,
        member_client: Client,
        member: OrganizationMember,
    ) -> None:
        """Should return 404 for a cancelled membership."""
        member.status = OrganizationMember.MembershipStatus.CANCELLED
        member.save(update_fields=["status"])

        url = reverse("api:me_membership_apple_wallet_pass", kwargs={"slug": member.organization.slug})
        response = member_client.get(url)

        assert response.status_code == 404

    def test_returns_404_for_banned_member(
        self,
        apple_wallet_configured: None,
        member_client: Client,
        member: OrganizationMember,
    ) -> None:
        """Should return 404 for a banned membership."""
        member.status = OrganizationMember.MembershipStatus.BANNED
        member.save(update_fields=["status"])

        url = reverse("api:me_membership_apple_wallet_pass", kwargs={"slug": member.organization.slug})
        response = member_client.get(url)

        assert response.status_code == 404

    def test_allows_paused_member(
        self,
        apple_wallet_configured: None,
        member_client: Client,
        member: OrganizationMember,
        mock_membership_pass_generator: MagicMock,
    ) -> None:
        """Should allow downloading passes for paused memberships."""
        member.status = OrganizationMember.MembershipStatus.PAUSED
        member.save(update_fields=["status"])

        url = reverse("api:me_membership_apple_wallet_pass", kwargs={"slug": member.organization.slug})
        response = member_client.get(url)

        assert response.status_code == 200


class TestMembershipGoogleWallet:
    """Tests for MembershipWalletController.google_wallet_save_link."""

    def test_bare_redirects_to_save_url(
        self,
        google_wallet_configured_settings: None,
        member_client: Client,
        member: OrganizationMember,
    ) -> None:
        """Should redirect to the Google Wallet save URL by default.

        The availability gate reads real settings (CI has none), so the
        configured-settings fixture is required — mocking membership_save_url
        alone is not enough.
        """
        save_url = "https://pay.google.com/gp/v/save/faketoken"
        with patch("wallet.controllers.google_wallet_service.membership_save_url", return_value=save_url):
            url = reverse("api:me_membership_google_wallet_pass", kwargs={"slug": member.organization.slug})
            response = member_client.get(url)

        assert response.status_code == 302
        assert response["Location"] == save_url

    def test_format_json_returns_save_url(
        self,
        google_wallet_configured_settings: None,
        member_client: Client,
        member: OrganizationMember,
    ) -> None:
        """Should return the save URL as JSON when ?format=json is passed."""
        save_url = "https://pay.google.com/gp/v/save/faketoken"
        with patch("wallet.controllers.google_wallet_service.membership_save_url", return_value=save_url):
            url = reverse("api:me_membership_google_wallet_pass", kwargs={"slug": member.organization.slug})
            response = member_client.get(url, {"format": "json"})

        assert response.status_code == 200
        assert response.json() == {"save_url": save_url}

    def test_returns_503_when_not_configured(
        self,
        google_wallet_not_configured: None,
        member_client: Client,
        member: OrganizationMember,
    ) -> None:
        """Should return 503 when Google Wallet is not configured."""
        url = reverse("api:me_membership_google_wallet_pass", kwargs={"slug": member.organization.slug})
        response = member_client.get(url)

        assert response.status_code == 503

    def test_returns_404_for_nonmember(
        self,
        nonmember_client: Client,
        member: OrganizationMember,
    ) -> None:
        """Should return 404 when the caller has no membership in the org."""
        url = reverse("api:me_membership_google_wallet_pass", kwargs={"slug": member.organization.slug})
        response = nonmember_client.get(url)

        assert response.status_code == 404

    def test_returns_401_for_unauthenticated_user(
        self,
        client: Client,
        member: OrganizationMember,
    ) -> None:
        """Should return 401 for unauthenticated requests."""
        url = reverse("api:me_membership_google_wallet_pass", kwargs={"slug": member.organization.slug})
        response = client.get(url)

        assert response.status_code == 401


class TestMembershipPdf:
    """Tests for MembershipWalletController.download_pdf."""

    def test_returns_pdf_with_correct_content_type(
        self,
        member_client: Client,
        member: OrganizationMember,
        mock_membership_pdf: MagicMock,
    ) -> None:
        """Should return PDF with application/pdf content type and attachment disposition."""
        url = reverse("api:me_membership_pdf", kwargs={"slug": member.organization.slug})
        response = member_client.get(url)

        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"
        assert "Content-Disposition" in response
        assert ".pdf" in response["Content-Disposition"]
        assert response.content == b"%PDF-fake"
        mock_membership_pdf.assert_called_once_with(member)

    def test_returns_404_for_nonmember(
        self,
        nonmember_client: Client,
        member: OrganizationMember,
    ) -> None:
        """Should return 404 when the caller has no membership in the org."""
        url = reverse("api:me_membership_pdf", kwargs={"slug": member.organization.slug})
        response = nonmember_client.get(url)

        assert response.status_code == 404

    def test_returns_401_for_unauthenticated_user(
        self,
        client: Client,
        member: OrganizationMember,
    ) -> None:
        """Should return 401 for unauthenticated requests."""
        url = reverse("api:me_membership_pdf", kwargs={"slug": member.organization.slug})
        response = client.get(url)

        assert response.status_code == 401

    def test_paused_member_can_download_pdf(
        self, member_client: t.Any, member: OrganizationMember, mock_membership_pdf: MagicMock
    ) -> None:
        """Paused memberships are still visible: PDF download should succeed."""
        member.status = OrganizationMember.MembershipStatus.PAUSED
        member.save(update_fields=["status"])
        response = member_client.get(f"/api/me/organizations/{member.organization.slug}/membership/pdf")
        assert response.status_code == 200

    def test_returns_404_for_cancelled_member(
        self,
        member_client: Client,
        member: OrganizationMember,
    ) -> None:
        """Should return 404 for a cancelled membership."""
        member.status = OrganizationMember.MembershipStatus.CANCELLED
        member.save(update_fields=["status"])

        url = reverse("api:me_membership_pdf", kwargs={"slug": member.organization.slug})
        response = member_client.get(url)

        assert response.status_code == 404

    def test_returns_404_for_banned_member(
        self,
        member_client: Client,
        member: OrganizationMember,
    ) -> None:
        """Should return 404 for a banned membership."""
        member.status = OrganizationMember.MembershipStatus.BANNED
        member.save(update_fields=["status"])

        url = reverse("api:me_membership_pdf", kwargs={"slug": member.organization.slug})
        response = member_client.get(url)

        assert response.status_code == 404


class TestSignedMembershipApplePass:
    """Tests for GET /api/memberships/{member_id}/wallet/apple/signed."""

    @staticmethod
    def _signed_url(member: OrganizationMember, expires: int) -> str:
        from common.signing import generate_signature

        path = reverse("api:membership_apple_wallet_signed", kwargs={"member_id": member.id})
        return f"{path}?exp={expires}&sig={generate_signature(path, expires)}"

    def test_valid_signature_serves_pkpass_without_auth(
        self,
        member: OrganizationMember,
        apple_wallet_configured: None,
        mock_membership_pass_generator: MagicMock,
    ) -> None:
        """A validly signed link should serve the pkpass without authentication."""
        expires = int(time.time()) + 3600
        response = Client().get(self._signed_url(member, expires))

        assert response.status_code == 200
        assert response["Content-Type"] == "application/vnd.apple.pkpass"
        assert response.content == b"mock_pkpass_content"

    def test_tampered_signature_returns_403(self, member: OrganizationMember, apple_wallet_configured: None) -> None:
        """A tampered signature should be rejected."""
        expires = int(time.time()) + 3600
        path = reverse("api:membership_apple_wallet_signed", kwargs={"member_id": member.id})
        response = Client().get(f"{path}?exp={expires}&sig=deadbeefdeadbeef")

        assert response.status_code == 403

    def test_expired_link_returns_410(self, member: OrganizationMember, apple_wallet_configured: None) -> None:
        """An expired link should return 410."""
        expires = int(time.time()) - 10
        response = Client().get(self._signed_url(member, expires))

        assert response.status_code == 410

    def test_malformed_exp_returns_403(self, member: OrganizationMember, apple_wallet_configured: None) -> None:
        """A non-integer exp should return 403, not 500."""
        path = reverse("api:membership_apple_wallet_signed", kwargs={"member_id": member.id})
        response = Client().get(f"{path}?exp=notanumber&sig=deadbeefdeadbeef")

        assert response.status_code == 403

    def test_unconfigured_returns_503(self, member: OrganizationMember, apple_wallet_not_configured: None) -> None:
        """Should return 503 when Apple Wallet is not configured."""
        expires = int(time.time()) + 3600
        response = Client().get(self._signed_url(member, expires))

        assert response.status_code == 503

    def test_cancelled_member_returns_404(
        self,
        member: OrganizationMember,
        apple_wallet_configured: None,
        mock_membership_pass_generator: MagicMock,
    ) -> None:
        """A cancelled membership should not be resolvable via the signed link."""
        member.status = OrganizationMember.MembershipStatus.CANCELLED
        member.save(update_fields=["status"])
        expires = int(time.time()) + 3600
        response = Client().get(self._signed_url(member, expires))

        assert response.status_code == 404

    def test_nonexistent_member_returns_404(self, apple_wallet_configured: None) -> None:
        """A signature for a nonexistent member id should return 404."""
        fake_id = uuid4()
        expires = int(time.time()) + 3600
        from common.signing import generate_signature

        path = reverse("api:membership_apple_wallet_signed", kwargs={"member_id": fake_id})
        response = Client().get(f"{path}?exp={expires}&sig={generate_signature(path, expires)}")

        assert response.status_code == 404
