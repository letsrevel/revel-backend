"""Tests for the RevelUser admin's Stripe Connect actions."""

import typing as t

import pytest
from django.contrib.admin.sites import site
from django.contrib.messages.storage.fallback import FallbackStorage

from accounts.models import RevelUser

pytestmark = pytest.mark.django_db


def _admin() -> t.Any:
    return site._registry[RevelUser]


def _request_with_messages(rf: t.Any, request_user: RevelUser) -> t.Any:
    request = rf.post("/")
    request.user = request_user
    request.session = {}
    request._messages = FallbackStorage(request)
    return request


@pytest.fixture
def connected_user(user: RevelUser) -> RevelUser:
    user.stripe_account_id = "acct_stale"
    user.stripe_charges_enabled = True
    user.stripe_details_submitted = True
    user.save(update_fields=["stripe_account_id", "stripe_charges_enabled", "stripe_details_submitted"])
    return user


def test_clear_stripe_connect_account_unlinks_stale_account(
    rf: t.Any, connected_user: RevelUser, superuser: RevelUser
) -> None:
    """A superuser can unlink an account that is gone at Stripe, enabling re-onboarding."""
    request = _request_with_messages(rf, superuser)

    _admin().clear_stripe_connect_account(request, RevelUser.objects.filter(pk=connected_user.pk))

    # Re-fetch: mypy narrows stripe_account_id to str after the fixture assignment,
    # so asserting None on the same instance reads as unreachable.
    fresh = RevelUser.objects.get(pk=connected_user.pk)
    assert fresh.stripe_account_id is None
    assert fresh.stripe_charges_enabled is False
    assert fresh.stripe_details_submitted is False


def test_clear_stripe_connect_account_requires_superuser(
    rf: t.Any, connected_user: RevelUser, django_user_model: t.Type[RevelUser]
) -> None:
    """Non-superusers cannot unlink an account, even with admin access."""
    staff_user = django_user_model.objects.create_user(
        username="staff@example.com", email="staff@example.com", password="strong-password-123!", is_staff=True
    )
    request = _request_with_messages(rf, staff_user)

    _admin().clear_stripe_connect_account(request, RevelUser.objects.filter(pk=connected_user.pk))

    connected_user.refresh_from_db()
    assert connected_user.stripe_account_id == "acct_stale"
    assert connected_user.stripe_charges_enabled is True
