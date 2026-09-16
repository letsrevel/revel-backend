"""Admin tests for ReferralApplication decisions and the invite page."""

import typing as t
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings
from django.test.client import Client
from django.urls import reverse

from accounts.models import ReferralApplication, ReferralCode, RevelUser

pytestmark = pytest.mark.django_db

# Rendering a full admin page needs a staticfiles manifest for whitenoise's storage;
# tests swap in the plain storage instead, matching events/tests/test_admin/test_organization_admin_filters.py.
NO_MANIFEST_STORAGE = override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)


@pytest.fixture
def admin_client(client: Client, superuser: RevelUser) -> Client:
    client.force_login(superuser)
    return client


@pytest.fixture(autouse=True)
def mock_email() -> t.Iterator[MagicMock]:
    with patch("accounts.tasks.send_account_email.delay") as m:
        yield m


@pytest.fixture
def pending() -> ReferralApplication:
    return ReferralApplication.objects.create(email="p@example.com", code="pcode", note="please")


def _change_url(app: ReferralApplication) -> str:
    return reverse("admin:accounts_referralapplication_change", args=[app.pk])


def _form_data(app: ReferralApplication, **overrides: str) -> dict[str, str]:
    data = {
        "code": app.code,
        "revenue_share_percent": str(app.revenue_share_percent),
        "admin_note": app.admin_note,
    }
    data.update(overrides)
    return data


class TestChangeForm:
    @NO_MANIFEST_STORAGE
    def test_pending_row_shows_three_buttons(self, admin_client: Client, pending: ReferralApplication) -> None:
        html = admin_client.get(_change_url(pending)).content.decode()
        for name in ("approve", "reject", "block"):
            assert f'name="accounts_referralapplication_{name}"' in html

    @NO_MANIFEST_STORAGE
    def test_decided_row_hides_buttons_and_locks_fields(
        self, admin_client: Client, pending: ReferralApplication
    ) -> None:
        pending.status = ReferralApplication.Status.REJECTED
        pending.save()
        html = admin_client.get(_change_url(pending)).content.decode()
        assert "accounts_referralapplication_approve" not in html
        assert 'name="code"' not in html

    @NO_MANIFEST_STORAGE
    def test_approve_button_with_edited_code(
        self, admin_client: Client, pending: ReferralApplication, mock_email: MagicMock
    ) -> None:
        data = _form_data(pending, code="edited", accounts_referralapplication_approve="1")
        response = admin_client.post(_change_url(pending), data, follow=True)
        assert response.status_code == 200
        pending.refresh_from_db()
        assert pending.status == ReferralApplication.Status.APPROVED and pending.code == "edited"

    @NO_MANIFEST_STORAGE
    def test_reject_button_stores_note(self, admin_client: Client, pending: ReferralApplication) -> None:
        data = _form_data(pending, admin_note="Not now", accounts_referralapplication_reject="1")
        admin_client.post(_change_url(pending), data, follow=True)
        pending.refresh_from_db()
        assert pending.status == ReferralApplication.Status.REJECTED and pending.admin_note == "Not now"

    @NO_MANIFEST_STORAGE
    def test_block_button(self, admin_client: Client, pending: ReferralApplication) -> None:
        data = _form_data(pending, accounts_referralapplication_block="1")
        admin_client.post(_change_url(pending), data, follow=True)
        pending.refresh_from_db()
        assert pending.status == ReferralApplication.Status.BLOCKED

    @NO_MANIFEST_STORAGE
    def test_approve_active_referrer_shows_error_and_stays_pending(
        self, admin_client: Client, pending: ReferralApplication, revel_user_factory: t.Any
    ) -> None:
        user = revel_user_factory(email="p@example.com")
        ReferralCode.objects.create(user=user, code="already")
        data = _form_data(pending, accounts_referralapplication_approve="1")
        response = admin_client.post(_change_url(pending), data, follow=True)
        assert "already has an active referral code" in response.content.decode()
        pending.refresh_from_db()
        assert pending.status == ReferralApplication.Status.PENDING

    @NO_MANIFEST_STORAGE
    def test_editing_code_to_a_taken_one_is_a_form_error(
        self, admin_client: Client, pending: ReferralApplication, revel_user_factory: t.Any
    ) -> None:
        ReferralCode.objects.create(user=revel_user_factory(), code="taken")
        response = admin_client.post(_change_url(pending), _form_data(pending, code="TAKEN"))
        assert response.status_code == 200
        assert "already taken" in response.content.decode()
        pending.refresh_from_db()
        assert pending.code == "pcode"

    def test_add_is_disabled(self, admin_client: Client) -> None:
        assert admin_client.get(reverse("admin:accounts_referralapplication_add")).status_code == 403


class TestChangelist:
    @NO_MANIFEST_STORAGE
    def test_lists_and_filters(self, admin_client: Client, pending: ReferralApplication) -> None:
        url = reverse("admin:accounts_referralapplication_changelist")
        assert admin_client.get(url).status_code == 200
        assert admin_client.get(url, {"status": "pending"}).status_code == 200
        assert admin_client.get(url, {"enrolled": "no"}).status_code == 200

    @NO_MANIFEST_STORAGE
    def test_pending_row_sorts_before_a_newer_decided_row(
        self, admin_client: Client, pending: ReferralApplication
    ) -> None:
        """An older PENDING row must outrank a newer decided one (get_ordering, not plain -created_at)."""
        older_pending = pending
        newer_decided = ReferralApplication.objects.create(
            email="d@example.com", code="dcode", note="please", status=ReferralApplication.Status.REJECTED
        )
        response = admin_client.get(reverse("admin:accounts_referralapplication_changelist"))
        result_list = list(response.context["cl"].result_list)
        assert [obj.pk for obj in result_list] == [older_pending.pk, newer_decided.pk]
