"""Tests for binding an Organization to the platform Stripe account."""

import typing as t

import pytest
from django.contrib.admin.sites import site
from django.contrib.messages.storage.fallback import FallbackStorage

from accounts.models import RevelUser
from events.admin.organization import OrganizationAdmin
from events.models import Organization

pytestmark = pytest.mark.django_db


def _admin() -> OrganizationAdmin:
    return t.cast(OrganizationAdmin, site._registry[Organization])


def _request_with_messages(rf: t.Any, user: RevelUser) -> t.Any:
    request = rf.post("/")
    request.user = user
    request.session = {}
    request._messages = FallbackStorage(request)
    return request


def test_bind_sets_platform_account(
    rf: t.Any, organization: Organization, superuser: RevelUser, settings: t.Any
) -> None:
    """Binding points the org at STRIPE_ACCOUNT with both flags enabled."""
    settings.STRIPE_ACCOUNT = "acct_platform"
    admin_instance = _admin()
    request = _request_with_messages(rf, superuser)

    admin_instance.bind_platform_stripe_account(request, Organization.objects.filter(pk=organization.pk))

    organization.refresh_from_db()
    assert organization.stripe_account_id == "acct_platform"
    assert organization.stripe_charges_enabled is True
    assert organization.stripe_details_submitted is True


def test_bind_refuses_already_connected_org(
    rf: t.Any, organization: Organization, superuser: RevelUser, settings: t.Any
) -> None:
    """An org with a real Connect account must not be silently rebound."""
    settings.STRIPE_ACCOUNT = "acct_platform"
    organization.stripe_account_id = "acct_real_connect"
    organization.save(update_fields=["stripe_account_id"])
    admin_instance = _admin()
    request = _request_with_messages(rf, superuser)

    admin_instance.bind_platform_stripe_account(request, Organization.objects.filter(pk=organization.pk))

    organization.refresh_from_db()
    assert organization.stripe_account_id == "acct_real_connect"  # unchanged


def test_bind_requires_superuser(
    rf: t.Any, organization: Organization, organization_owner_user: RevelUser, settings: t.Any
) -> None:
    """Non-superusers cannot bind, even with admin access."""
    settings.STRIPE_ACCOUNT = "acct_platform"
    admin_instance = _admin()
    request = _request_with_messages(rf, organization_owner_user)

    admin_instance.bind_platform_stripe_account(request, Organization.objects.filter(pk=organization.pk))

    organization.refresh_from_db()
    assert organization.stripe_account_id is None


def test_bind_requires_exactly_one_org(
    rf: t.Any, organization: Organization, superuser: RevelUser, settings: t.Any
) -> None:
    """Selecting zero or multiple orgs is refused."""
    settings.STRIPE_ACCOUNT = "acct_platform"
    admin_instance = _admin()
    request = _request_with_messages(rf, superuser)

    admin_instance.bind_platform_stripe_account(request, Organization.objects.none())

    organization.refresh_from_db()
    assert organization.stripe_account_id is None


def test_unbind_clears_platform_binding(
    rf: t.Any, organization: Organization, superuser: RevelUser, settings: t.Any
) -> None:
    """Unbinding a platform-bound org clears the account and flags."""
    settings.STRIPE_ACCOUNT = "acct_platform"
    organization.stripe_account_id = "acct_platform"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.save(update_fields=["stripe_account_id", "stripe_charges_enabled", "stripe_details_submitted"])
    admin_instance = _admin()
    request = _request_with_messages(rf, superuser)

    admin_instance.unbind_platform_stripe_account(request, Organization.objects.filter(pk=organization.pk))

    # Re-fetch: mypy narrows stripe_account_id to str after the assignment
    # above, so asserting None on the same instance reads as unreachable.
    fresh = Organization.objects.get(pk=organization.pk)
    assert fresh.stripe_account_id is None
    assert fresh.stripe_charges_enabled is False
    assert fresh.stripe_details_submitted is False


def test_unbind_refuses_real_connect_binding(
    rf: t.Any, organization: Organization, superuser: RevelUser, settings: t.Any
) -> None:
    """A real Connect binding must never be cleared by the unbind action."""
    settings.STRIPE_ACCOUNT = "acct_platform"
    organization.stripe_account_id = "acct_real_connect"
    organization.stripe_charges_enabled = True
    organization.save(update_fields=["stripe_account_id", "stripe_charges_enabled"])
    admin_instance = _admin()
    request = _request_with_messages(rf, superuser)

    admin_instance.unbind_platform_stripe_account(request, Organization.objects.filter(pk=organization.pk))

    organization.refresh_from_db()
    assert organization.stripe_account_id == "acct_real_connect"  # untouched
    assert organization.stripe_charges_enabled is True
