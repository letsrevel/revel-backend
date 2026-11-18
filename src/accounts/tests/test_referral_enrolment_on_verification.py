"""An approved referral invitee is enrolled when their account becomes verified, whichever path does it."""

import typing as t
from unittest.mock import MagicMock, patch

import pytest

from accounts.models import ReferralApplication, ReferralCode, RevelUser
from accounts.service import account as account_service
from accounts.service import oidc as oidc_service

pytestmark = pytest.mark.django_db

EMAIL = "invitee@example.com"


@pytest.fixture
def application() -> ReferralApplication:
    return ReferralApplication.objects.create(
        email=EMAIL, code="invited", note="hi", status=ReferralApplication.Status.APPROVED
    )


@pytest.fixture
def unverified(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username=EMAIL, email=EMAIL, password="x", email_verified=False)


@pytest.fixture(autouse=True)
def _mute_email() -> t.Iterator[MagicMock]:
    with patch("accounts.tasks.send_account_email.delay") as m:
        yield m


def _enrolled(application: ReferralApplication, user: RevelUser) -> bool:
    application.refresh_from_db()
    return application.user_id == user.pk and ReferralCode.objects.filter(user=user, code="invited").exists()


def test_unverified_account_is_not_enrolled(application: ReferralApplication, unverified: RevelUser) -> None:
    assert not _enrolled(application, unverified)


def test_account_created_verified_is_enrolled(
    application: ReferralApplication, django_user_model: type[RevelUser]
) -> None:
    user = django_user_model.objects.create_user(username=EMAIL, email=EMAIL, password="x", email_verified=True)
    assert _enrolled(application, user)


def test_admin_style_verified_save_enrols(application: ReferralApplication, unverified: RevelUser) -> None:
    unverified.email_verified = True
    unverified.save()
    assert _enrolled(application, unverified)


def test_oidc_adoption_of_an_unverified_invitee_enrols(application: ReferralApplication, unverified: RevelUser) -> None:
    oidc_service._adopt_unverified_account(unverified)  # noqa: SLF001 - the unit under test is the private step
    assert _enrolled(application, unverified)


def test_email_change_confirm_on_an_unverified_account_enrols(
    unverified: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    changed = ReferralApplication.objects.create(
        email="new@example.com", code="invited", note="hi", status=ReferralApplication.Status.APPROVED
    )
    with django_capture_on_commit_callbacks(execute=True):
        token = account_service.create_email_change_token(unverified, "new@example.com")

    account_service.confirm_email_change(token)

    unverified.refresh_from_db()
    assert unverified.email_verified is True
    assert _enrolled(changed, unverified)


def test_verified_save_does_not_enrol_twice(application: ReferralApplication, unverified: RevelUser) -> None:
    unverified.email_verified = True
    unverified.save()
    unverified.first_name = "Changed"
    unverified.save()
    assert ReferralCode.objects.filter(user=unverified).count() == 1


def test_verification_link_enrols_once(application: ReferralApplication, unverified: RevelUser) -> None:
    account_service.verify_email(account_service.create_verification_token(unverified))
    assert _enrolled(application, unverified)
    assert ReferralCode.objects.filter(user=unverified).count() == 1
