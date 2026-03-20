"""Tests for get_org_token_rejection_reason() in events.service.organization_service."""

from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Organization, OrganizationToken
from events.service.organization_service import OrgTokenRejection, get_org_token_rejection_reason

pytestmark = pytest.mark.django_db


@pytest.fixture
def _org_owner(django_user_model: type[RevelUser]) -> RevelUser:
    """Owner user for org-token-rejection tests."""
    return django_user_model.objects.create_user(
        username="org_tok_rej_owner",
        email="org_tok_rej_owner@example.com",
        password="pass",
    )


@pytest.fixture
def _org(_org_owner: RevelUser) -> Organization:
    """Organization for org-token-rejection tests."""
    return Organization.objects.create(
        name="Org Tok Rej Org",
        slug="org-tok-rej-org",
        owner=_org_owner,
    )


def test_expired_token_returns_expired_reason(
    _org: Organization,
    _org_owner: RevelUser,
) -> None:
    """An expired org token is diagnosed as reason='expired' with the correct organization_id."""
    # Arrange
    token = OrganizationToken.objects.create(
        organization=_org,
        issuer=_org_owner,
        grants_membership=False,
        expires_at=timezone.now() - timedelta(hours=1),
    )

    # Act
    result = get_org_token_rejection_reason(token.pk)

    # Assert
    assert result is not None
    assert isinstance(result, OrgTokenRejection)
    assert result.reason == "expired"
    assert result.organization_id == _org.pk


def test_used_up_token_returns_used_up_reason(
    _org: Organization,
    _org_owner: RevelUser,
) -> None:
    """An org token that has reached its max_uses is diagnosed as reason='used_up'."""
    # Arrange
    token = OrganizationToken.objects.create(
        organization=_org,
        issuer=_org_owner,
        grants_membership=False,
        expires_at=timezone.now() + timedelta(hours=1),
        max_uses=5,
        uses=5,
    )

    # Act
    result = get_org_token_rejection_reason(token.pk)

    # Assert
    assert result is not None
    assert isinstance(result, OrgTokenRejection)
    assert result.reason == "used_up"
    assert result.organization_id == _org.pk


def test_nonexistent_token_returns_none() -> None:
    """A completely nonexistent org token returns None (genuine 404, not 410)."""
    # Act
    result = get_org_token_rejection_reason("nonexistent-org-token-12345")

    # Assert
    assert result is None


def test_valid_token_returns_none(
    _org: Organization,
    _org_owner: RevelUser,
) -> None:
    """A still-valid org token (not expired, uses below max) returns None.

    This happens when get_organization_token() returned None for another reason
    (e.g., a race condition or code bug). The function should not fabricate
    a rejection reason for a healthy token.
    """
    # Arrange
    token = OrganizationToken.objects.create(
        organization=_org,
        issuer=_org_owner,
        grants_membership=False,
        expires_at=timezone.now() + timedelta(hours=1),
        max_uses=10,
        uses=3,
    )

    # Act
    result = get_org_token_rejection_reason(token.pk)

    # Assert
    assert result is None
