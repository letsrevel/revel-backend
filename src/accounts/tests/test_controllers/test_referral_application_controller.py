"""Integration tests for the public referral application endpoints."""

import typing as t
from unittest.mock import MagicMock, patch

import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import ReferralApplication, ReferralCode, RevelUser
from common.models import SiteSettings

pytestmark = pytest.mark.django_db

APPLY_URL = reverse("api:referral-apply")
VALID = {"email": "Someone@Example.com", "code": "my-code", "note": "I organize a weekly meetup"}


@pytest.fixture
def applications_enabled() -> t.Iterator[None]:
    site = SiteSettings.get_solo()
    site.referral_applications_enabled = True
    site.save()
    yield
    site.referral_applications_enabled = False
    site.save()


@pytest.fixture(autouse=True)
def _mock_dispatch() -> t.Iterator[MagicMock]:
    with (
        patch("accounts.tasks.send_account_email.delay"),
        patch("accounts.tasks.notify_admin_new_referral_application.delay") as m,
    ):
        yield m


def _post(client: Client, payload: dict[str, str]) -> t.Any:
    return client.post(APPLY_URL, payload, content_type="application/json")


class TestApply:
    def test_disabled_returns_404(self, client: Client) -> None:
        response = _post(client, VALID)
        assert response.status_code == 404
        assert response.json() == {"detail": "Referral applications are not open."}

    def test_success_returns_202_and_creates_pending(self, client: Client, applications_enabled: None) -> None:
        response = _post(client, VALID)
        assert response.status_code == 202, response.content
        assert response.json() == {"status": "ok"}
        app = ReferralApplication.objects.get()
        assert app.email == "someone@example.com" and app.code == "my-code"

    @pytest.mark.parametrize(
        "bad",
        [
            {**VALID, "email": "nope"},
            {**VALID, "code": "no spaces"},
            {**VALID, "code": "ab"},
            {**VALID, "note": ""},
            {**VALID, "note": "x" * 2001},
        ],
    )
    def test_validation_errors_are_422(self, client: Client, applications_enabled: None, bad: dict[str, str]) -> None:
        assert _post(client, bad).status_code == 422

    def test_pending_duplicate_is_409(self, client: Client, applications_enabled: None) -> None:
        assert _post(client, VALID).status_code == 202
        response = _post(client, {**VALID, "code": "other"})
        assert response.status_code == 409
        assert response.json() == {"detail": "You already have a pending application."}

    def test_taken_code_is_409(self, client: Client, applications_enabled: None, revel_user_factory: t.Any) -> None:
        ReferralCode.objects.create(user=revel_user_factory(), code="MY-CODE")
        response = _post(client, VALID)
        assert response.status_code == 409
        assert response.json() == {"detail": "This referral code is already taken."}

    def test_blocked_email_returns_202_without_creating(self, client: Client, applications_enabled: None) -> None:
        ReferralApplication.objects.create(
            email="someone@example.com", code="old", note="hi", status=ReferralApplication.Status.BLOCKED
        )
        assert _post(client, VALID).status_code == 202
        assert not ReferralApplication.objects.filter(status=ReferralApplication.Status.PENDING).exists()


class TestApplyThrottle:
    @pytest.fixture(autouse=True)
    def _enable_throttling(self, settings: t.Any) -> None:
        settings.DISABLE_THROTTLING = False

    def test_eleventh_request_per_day_is_429(self, client: Client, applications_enabled: None) -> None:
        for _ in range(10):
            # 404/409/422 all count; use a validation error so nothing is created
            assert _post(client, {**VALID, "note": ""}).status_code == 422
        assert _post(client, {**VALID, "note": ""}).status_code == 429


class TestInvitationLookup:
    def _url(self, app: ReferralApplication) -> str:
        return reverse("api:referral-invitation", kwargs={"application_id": app.id})

    def test_approved_unenrolled_returns_email_and_code(self, client: Client) -> None:
        app = ReferralApplication.objects.create(
            email="inv@example.com", code="inv", note="hi", status=ReferralApplication.Status.APPROVED
        )
        response = client.get(self._url(app))
        assert response.status_code == 200
        assert response.json() == {"email": "inv@example.com", "code": "inv"}

    @pytest.mark.parametrize("status", [ReferralApplication.Status.PENDING, ReferralApplication.Status.REJECTED])
    def test_not_approved_is_404(self, client: Client, status: str) -> None:
        app = ReferralApplication.objects.create(email="inv@example.com", code="inv", note="hi", status=status)
        assert client.get(self._url(app)).status_code == 404

    def test_enrolled_is_404(self, client: Client, revel_user_factory: t.Any) -> None:
        user: RevelUser = revel_user_factory(email="inv@example.com")
        app = ReferralApplication.objects.create(
            email="inv@example.com", code="inv", note="hi", status=ReferralApplication.Status.APPROVED, user=user
        )
        assert client.get(self._url(app)).status_code == 404

    def test_unknown_id_is_404(self, client: Client) -> None:
        url = reverse("api:referral-invitation", kwargs={"application_id": "00000000-0000-0000-0000-000000000000"})
        assert client.get(url).status_code == 404
