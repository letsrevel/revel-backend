"""Shared fixtures for the register linking tests (#1319)."""

import typing as t

import pytest
from django.test.client import Client
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import MembershipRecord, MembershipTier, Organization, OrganizationStaff

RecordFactory = t.Callable[..., MembershipRecord]


def make_client(user: RevelUser) -> Client:
    """A test client authenticated as ``user``."""
    refresh = RefreshToken.for_user(user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def link_tier(organization: Organization) -> MembershipTier:
    """A tier in the organization."""
    return MembershipTier.objects.create(organization=organization, name="Linking Gold")


@pytest.fixture
def make_record(organization: Organization, link_tier: MembershipTier) -> RecordFactory:
    """Create an account-less open STANDARD record (override any field through kwargs)."""

    def _make(**extra: t.Any) -> MembershipRecord:
        fields: dict[str, t.Any] = {
            "organization": organization,
            "user": None,
            "full_name": "Pat Register",
            "email": "pat@example.com",
            "tier": link_tier,
            "tier_name": link_tier.name,
            "valid_from": timezone.now(),
            "source": MembershipRecord.Source.MANUAL,
        }
        fields.update(extra)
        return MembershipRecord.objects.create(**fields)

    return _make


@pytest.fixture
def claimant(django_user_model: type[RevelUser]) -> RevelUser:
    """A verified account whose email differs from the record's."""
    return django_user_model.objects.create_user(
        username="claimant@example.com", email="claimant@example.com", password="pw", email_verified=True
    )


@pytest.fixture
def owner_client(organization_owner_user: RevelUser) -> Client:
    """Client of the organization owner."""
    return make_client(organization_owner_user)


@pytest.fixture
def staff_client(organization_staff_user: RevelUser, organization: Organization) -> Client:
    """Client of staff with manage_members."""
    OrganizationStaff.objects.create(
        organization=organization, user=organization_staff_user, permissions={"default": {"manage_members": True}}
    )
    return make_client(organization_staff_user)


@pytest.fixture
def plain_staff_client(organization_staff_user: RevelUser, organization: Organization) -> Client:
    """Client of staff without manage_members."""
    OrganizationStaff.objects.create(
        organization=organization, user=organization_staff_user, permissions={"default": {"manage_members": False}}
    )
    return make_client(organization_staff_user)


@pytest.fixture
def outsider_client(django_user_model: type[RevelUser]) -> Client:
    """Client of an unrelated user."""
    return make_client(django_user_model.objects.create_user(username="out", email="out@example.com", password="pw"))
