"""Fixtures for the TEMPORARY membership tier tests."""

import pytest
from django.test.client import Client
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import MembershipTier, Organization


@pytest.fixture
def standard_tier(organization: Organization) -> MembershipTier:
    """A regular (holdable) tier."""
    return MembershipTier.objects.create(organization=organization, name="Regular")


@pytest.fixture
def temp_tier(organization: Organization) -> MembershipTier:
    """A TEMPORARY day-membership tier."""
    return MembershipTier.objects.create(organization=organization, name="Day pass", kind=MembershipTier.Kind.TEMPORARY)


@pytest.fixture
def public_org(organization: Organization) -> Organization:
    """The organization, made publicly visible."""
    organization.visibility = Organization.Visibility.PUBLIC
    organization.save(update_fields=["visibility"])
    return organization


@pytest.fixture
def nonmember_api_client(nonmember_user: RevelUser) -> Client:
    """Authenticated client for a user without any org relationship."""
    refresh = RefreshToken.for_user(nonmember_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")  # type: ignore[attr-defined]
