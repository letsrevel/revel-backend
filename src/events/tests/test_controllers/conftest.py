import typing as t

import pytest
from django.test.client import Client
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Organization, OrganizationMember, OrganizationStaff


@pytest.fixture
def superuser(django_user_model: t.Type[RevelUser]) -> RevelUser:
    """A superuser."""
    return django_user_model.objects.create_superuser(username="super", email="super@example.com", password="pass")


@pytest.fixture
def superuser_client(superuser: RevelUser) -> Client:
    """API client for a superuser."""
    refresh = RefreshToken.for_user(superuser)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def organization_owner_client(organization_owner_user: RevelUser) -> Client:
    """API client for an organization owner."""
    refresh = RefreshToken.for_user(organization_owner_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def organization_staff_client(organization_staff_user: RevelUser, staff_member: OrganizationStaff) -> Client:
    """API client for an organization staff member."""
    refresh = RefreshToken.for_user(organization_staff_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def member_client(member_user: RevelUser, organization: Organization) -> Client:
    """API client for a standard organization member."""
    OrganizationMember.objects.create(organization=organization, user=member_user)
    refresh = RefreshToken.for_user(member_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def nonmember_client(nonmember_user: RevelUser) -> Client:
    """API client for an authenticated user with no specific org relationship."""
    refresh = RefreshToken.for_user(nonmember_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]
