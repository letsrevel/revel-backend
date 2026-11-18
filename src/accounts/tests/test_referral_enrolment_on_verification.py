"""An approved referral invitee is enrolled when their account becomes verified, whichever path does it."""

import typing as t
from unittest.mock import MagicMock, patch

import pytest

from accounts.models import ReferralApplication, ReferralCode, RevelUser
from accounts.service import account as account_service
from accounts.service import oidc as oidc_service
from accounts.service import referral_application_service

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
    application.refresh_from_db()
    assert application.user_id is None
    assert not ReferralCode.objects.filter(user=unverified).exists()


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


@pytest.fixture
def spy() -> t.Iterator[MagicMock]:
    """Spy on the enrolment call the receiver makes (it is idempotent, so only a spy shows the transition gate)."""
    with patch(
        "accounts.service.referral_application_service.try_enroll_invitee",
        wraps=referral_application_service.try_enroll_invitee,
    ) as m:
        yield m


def test_enrolment_runs_once_on_the_transition_only(
    application: ReferralApplication, unverified: RevelUser, spy: MagicMock
) -> None:
    unverified.email_verified = True
    unverified.save()
    assert spy.call_count == 1

    unverified.first_name = "Changed"
    unverified.save()  # full save of an already-verified user
    unverified.save(update_fields=["email_verified"])  # field list naming email_verified, value unchanged
    unverified.save(update_fields=["first_name"])
    assert spy.call_count == 1


def test_update_fields_verification_enrols_once(
    application: ReferralApplication, unverified: RevelUser, spy: MagicMock
) -> None:
    unverified.email_verified = True
    unverified.save(update_fields=["email_verified"])
    unverified.save(update_fields=["email_verified"])
    assert spy.call_count == 1
    assert _enrolled(application, unverified)


def test_guest_verified_save_is_not_enrolled_but_conversion_is(
    application: ReferralApplication, django_user_model: type[RevelUser], spy: MagicMock
) -> None:
    guest = django_user_model.objects.create(username=EMAIL, email=EMAIL, guest=True, email_verified=False)
    guest.email_verified = True
    guest.save()
    spy.assert_not_called()
    assert not _enrolled(application, guest)

    assert account_service.convert_guest(guest) is True

    assert spy.call_count == 1
    assert _enrolled(application, guest)


def test_verification_link_enrols_once(application: ReferralApplication, unverified: RevelUser) -> None:
    account_service.verify_email(account_service.create_verification_token(unverified))
    assert _enrolled(application, unverified)
    assert ReferralCode.objects.filter(user=unverified).count() == 1
