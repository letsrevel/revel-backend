"""Tests for user referral payout and statement endpoints."""

import typing as t
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.conf import settings
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import (
    Referral,
    ReferralCode,
    ReferralPayout,
    ReferralPayoutStatement,
    RevelUser,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def referrer(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="referrer@example.com",
        email="referrer@example.com",
        password="strong-password-123!",
    )


@pytest.fixture
def referred_user(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="referred@example.com",
        email="referred@example.com",
        password="strong-password-123!",
    )


@pytest.fixture
def other_user(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="other@example.com",
        email="other@example.com",
        password="strong-password-123!",
    )


@pytest.fixture
def referral_code(referrer: RevelUser) -> ReferralCode:
    return ReferralCode.objects.create(user=referrer, code="REFPAY")


@pytest.fixture
def referral(referral_code: ReferralCode, referred_user: RevelUser) -> Referral:
    return Referral.objects.create(
        referral_code=referral_code,
        referred_user=referred_user,
        revenue_share_percent=Decimal("20.00"),
    )


@pytest.fixture
def referrer_client(referrer: RevelUser) -> Client:
    refresh = RefreshToken.for_user(referrer)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def other_client(other_user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(other_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_payout(
    referral: Referral,
    *,
    period_start: date | None = None,
    status: str = ReferralPayout.Status.PAID,
) -> ReferralPayout:
    start = period_start or date(2026, 1, 1)
    return ReferralPayout.objects.create(
        referral=referral,
        period_start=start,
        period_end=start + timedelta(days=30),
        net_platform_fees=Decimal("100.00"),
        payout_amount=Decimal("20.00"),
        currency=settings.DEFAULT_CURRENCY,
        status=status,
    )


def _create_statement(
    payout: ReferralPayout,
    *,
    number_suffix: str = "001",
    with_pdf: bool = False,
) -> ReferralPayoutStatement:
    statement = ReferralPayoutStatement.objects.create(
        payout=payout,
        document_type=ReferralPayoutStatement.DocumentType.SELF_BILLING_INVOICE,
        document_number=f"RVL-RP-2026-{number_suffix}",
        amount_gross=Decimal("23.80"),
        amount_net=Decimal("20.00"),
        amount_vat=Decimal("3.80"),
        vat_rate=Decimal("19.00"),
        currency=settings.DEFAULT_CURRENCY,
        reverse_charge=False,
        referrer_name="Test Referrer",
        referrer_address="123 Test St",
        referrer_vat_id="DE123456789",
        referrer_country="DE",
        platform_business_name="Revel GmbH",
        platform_business_address="Vienna, Austria",
        platform_vat_id="ATU12345678",
        issued_at=timezone.now(),
    )
    if with_pdf:
        statement.pdf_file.name = "invoices/referral_payouts/test.pdf"
        statement.save(update_fields=["pdf_file"])
    return statement


# ===========================================================================
# GET /me/referral/payouts
# ===========================================================================


class TestListPayouts:
    url = reverse("api:list_referral_payouts")

    def test_unauthenticated_returns_401(self, client: Client) -> None:
        response = client.get(self.url)
        assert response.status_code == 401

    def test_user_with_no_referrals_gets_empty_list(self, other_client: Client) -> None:
        response = other_client.get(self.url)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 0
        assert data["results"] == []

    def test_referrer_can_list_payouts(
        self,
        referrer_client: Client,
        referral: Referral,
    ) -> None:
        _create_payout(referral, period_start=date(2026, 1, 1))
        _create_payout(referral, period_start=date(2026, 2, 1))

        response = referrer_client.get(self.url)

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        assert len(data["results"]) == 2

    def test_payout_fields_in_response(
        self,
        referrer_client: Client,
        referral: Referral,
    ) -> None:
        payout = _create_payout(referral)

        response = referrer_client.get(self.url)

        assert response.status_code == 200
        result = response.json()["results"][0]
        assert result["id"] == str(payout.id)
        assert result["period_start"] == "2026-01-01"
        assert result["net_platform_fees"] == "100.00"
        assert result["payout_amount"] == "20.00"
        assert result["status"] == "paid"
        assert result["has_statement"] is False

    def test_has_statement_true_when_statement_exists(
        self,
        referrer_client: Client,
        referral: Referral,
    ) -> None:
        payout = _create_payout(referral)
        _create_statement(payout)

        response = referrer_client.get(self.url)

        assert response.status_code == 200
        result = response.json()["results"][0]
        assert result["has_statement"] is True

    def test_payouts_ordered_by_period_start_descending(
        self,
        referrer_client: Client,
        referral: Referral,
    ) -> None:
        _create_payout(referral, period_start=date(2026, 1, 1))
        _create_payout(referral, period_start=date(2026, 3, 1))
        _create_payout(referral, period_start=date(2026, 2, 1))

        response = referrer_client.get(self.url)

        assert response.status_code == 200
        dates = [r["period_start"] for r in response.json()["results"]]
        assert dates == ["2026-03-01", "2026-02-01", "2026-01-01"]

    def test_payouts_scoped_to_user(
        self,
        referrer_client: Client,
        referral: Referral,
        other_user: RevelUser,
        django_user_model: type[RevelUser],
    ) -> None:
        """Payouts from other referrers are not visible."""
        _create_payout(referral, period_start=date(2026, 1, 1))

        # Create another referrer's payout with a distinct referred user
        another_referred = django_user_model.objects.create_user(
            username="another-referred@example.com",
            email="another-referred@example.com",
            password="strong-password-123!",
        )
        other_code = ReferralCode.objects.create(user=other_user, code="OTHER")
        other_referral = Referral.objects.create(
            referral_code=other_code,
            referred_user=another_referred,
            revenue_share_percent=Decimal("10.00"),
        )
        _create_payout(other_referral, period_start=date(2026, 2, 1))

        response = referrer_client.get(self.url)

        assert response.status_code == 200
        assert response.json()["count"] == 1

    def test_payouts_aggregated_across_multiple_referrals(
        self,
        referrer_client: Client,
        referral_code: ReferralCode,
        referral: Referral,
        django_user_model: type[RevelUser],
    ) -> None:
        """A referrer who referred multiple users sees payouts from all referrals."""
        _create_payout(referral, period_start=date(2026, 1, 1))

        # Same referrer & code, second referred user
        second_referred = django_user_model.objects.create_user(
            username="second-referred@example.com",
            email="second-referred@example.com",
            password="strong-password-123!",
        )
        second_referral = Referral.objects.create(
            referral_code=referral_code,
            referred_user=second_referred,
            revenue_share_percent=Decimal("15.00"),
        )
        _create_payout(second_referral, period_start=date(2026, 2, 1))

        response = referrer_client.get(self.url)

        assert response.status_code == 200
        assert response.json()["count"] == 2

    def test_payouts_paginated(
        self,
        referrer_client: Client,
        referral: Referral,
    ) -> None:
        for i in range(25):
            _create_payout(referral, period_start=date(2020, 1, 1) + timedelta(days=31 * i))

        response = referrer_client.get(self.url)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 25
        assert len(data["results"]) == 20

        response = referrer_client.get(self.url, {"page": 2})
        assert response.status_code == 200
        assert len(response.json()["results"]) == 5


# ===========================================================================
# GET /me/referral/payouts/{payout_id}/statement
# ===========================================================================


class TestGetStatement:
    def _url(self, payout_id: str) -> str:
        return reverse("api:get_payout_statement", kwargs={"payout_id": payout_id})

    def test_unauthenticated_returns_401(self, client: Client, referral: Referral) -> None:
        payout = _create_payout(referral)
        response = client.get(self._url(str(payout.id)))
        assert response.status_code == 401

    def test_referrer_can_get_statement(
        self,
        referrer_client: Client,
        referral: Referral,
    ) -> None:
        payout = _create_payout(referral)
        statement = _create_statement(payout)

        response = referrer_client.get(self._url(str(payout.id)))

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(statement.id)
        assert data["document_number"] == "RVL-RP-2026-001"
        assert data["document_type"] == "self_billing_invoice"
        assert data["amount_gross"] == "23.80"
        assert data["amount_net"] == "20.00"
        assert data["amount_vat"] == "3.80"
        assert data["vat_rate"] == "19.00"
        assert data["reverse_charge"] is False
        assert data["issued_at"] is not None

    def test_payout_without_statement_returns_404(
        self,
        referrer_client: Client,
        referral: Referral,
    ) -> None:
        payout = _create_payout(referral)

        response = referrer_client.get(self._url(str(payout.id)))

        assert response.status_code == 404

    def test_other_users_payout_returns_404(
        self,
        other_client: Client,
        referral: Referral,
    ) -> None:
        payout = _create_payout(referral)
        _create_statement(payout)

        response = other_client.get(self._url(str(payout.id)))

        assert response.status_code == 404

    def test_nonexistent_payout_returns_404(self, referrer_client: Client) -> None:
        response = referrer_client.get(self._url(str(uuid4())))
        assert response.status_code == 404


# ===========================================================================
# GET /me/referral/payouts/{payout_id}/statement/download
# ===========================================================================


class TestDownloadStatement:
    def _url(self, payout_id: str) -> str:
        return reverse("api:download_payout_statement", kwargs={"payout_id": payout_id})

    def test_unauthenticated_returns_401(self, client: Client, referral: Referral) -> None:
        payout = _create_payout(referral)
        response = client.get(self._url(str(payout.id)))
        assert response.status_code == 401

    @patch("accounts.controllers.referral_payouts.get_file_url")
    def test_referrer_can_download_statement_pdf(
        self,
        mock_get_file_url: t.Any,
        referrer_client: Client,
        referral: Referral,
    ) -> None:
        mock_get_file_url.return_value = "https://cdn.example.com/signed/statement.pdf?sig=abc"
        payout = _create_payout(referral)
        _create_statement(payout, with_pdf=True)

        response = referrer_client.get(self._url(str(payout.id)))

        assert response.status_code == 200
        data = response.json()
        assert data["download_url"] == "https://cdn.example.com/signed/statement.pdf?sig=abc"
        mock_get_file_url.assert_called_once()

    @patch("accounts.controllers.referral_payouts.get_file_url")
    def test_statement_without_pdf_returns_404(
        self,
        mock_get_file_url: t.Any,
        referrer_client: Client,
        referral: Referral,
    ) -> None:
        mock_get_file_url.return_value = None
        payout = _create_payout(referral)
        _create_statement(payout)

        response = referrer_client.get(self._url(str(payout.id)))

        assert response.status_code == 404

    def test_payout_without_statement_returns_404(
        self,
        referrer_client: Client,
        referral: Referral,
    ) -> None:
        payout = _create_payout(referral)

        response = referrer_client.get(self._url(str(payout.id)))

        assert response.status_code == 404

    def test_other_users_payout_returns_404(
        self,
        other_client: Client,
        referral: Referral,
    ) -> None:
        payout = _create_payout(referral)
        _create_statement(payout, with_pdf=True)

        response = other_client.get(self._url(str(payout.id)))

        assert response.status_code == 404

    def test_nonexistent_payout_returns_404(self, referrer_client: Client) -> None:
        response = referrer_client.get(self._url(str(uuid4())))
        assert response.status_code == 404
