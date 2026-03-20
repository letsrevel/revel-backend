"""Tests for get_token_rejection_reason() in events.service.tokens."""

from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, EventToken, Organization
from events.service.tokens import TokenRejection, get_token_rejection_reason

pytestmark = pytest.mark.django_db


@pytest.fixture
def _org_owner(django_user_model: type[RevelUser]) -> RevelUser:
    """Owner user for token-rejection tests."""
    return django_user_model.objects.create_user(
        username="tok_rej_owner",
        email="tok_rej_owner@example.com",
        password="pass",
    )


@pytest.fixture
def _org(_org_owner: RevelUser) -> Organization:
    """Organization for token-rejection tests."""
    return Organization.objects.create(
        name="Tok Rej Org",
        slug="tok-rej-org",
        owner=_org_owner,
    )


@pytest.fixture
def _event(_org: Organization) -> Event:
    """Event for token-rejection tests."""
    return Event.objects.create(
        organization=_org,
        name="Tok Rej Event",
        slug="tok-rej-event",
        visibility=Event.Visibility.PRIVATE,
        event_type=Event.EventType.PRIVATE,
        status="open",
        start=timezone.now() + timedelta(days=7),
    )


def test_expired_token_returns_expired_reason(
    _event: Event,
    _org_owner: RevelUser,
) -> None:
    """An expired token is diagnosed as reason='expired' with the correct event_id."""
    # Arrange
    token = EventToken.objects.create(
        event=_event,
        issuer=_org_owner,
        expires_at=timezone.now() - timedelta(hours=1),
    )

    # Act
    result = get_token_rejection_reason(token.pk)

    # Assert
    assert result is not None
    assert isinstance(result, TokenRejection)
    assert result.reason == "expired"
    assert result.event_id == _event.pk


def test_used_up_token_returns_used_up_reason(
    _event: Event,
    _org_owner: RevelUser,
) -> None:
    """A token that has reached its max_uses is diagnosed as reason='used_up'."""
    # Arrange
    token = EventToken.objects.create(
        event=_event,
        issuer=_org_owner,
        expires_at=timezone.now() + timedelta(hours=1),
        max_uses=5,
        uses=5,
    )

    # Act
    result = get_token_rejection_reason(token.pk)

    # Assert
    assert result is not None
    assert isinstance(result, TokenRejection)
    assert result.reason == "used_up"
    assert result.event_id == _event.pk


def test_nonexistent_token_returns_none() -> None:
    """A completely nonexistent token returns None (genuine 404, not 410)."""
    # Act
    result = get_token_rejection_reason("nonexistent-token-id-12345")

    # Assert
    assert result is None


def test_valid_token_returns_none(
    _event: Event,
    _org_owner: RevelUser,
) -> None:
    """A still-valid token (not expired, uses below max) returns None.

    This happens when get_event_token() returned None for another reason
    (e.g., a race condition or code bug). The function should not fabricate
    a rejection reason for a healthy token.
    """
    # Arrange
    token = EventToken.objects.create(
        event=_event,
        issuer=_org_owner,
        expires_at=timezone.now() + timedelta(hours=1),
        max_uses=10,
        uses=3,
    )

    # Act
    result = get_token_rejection_reason(token.pk)

    # Assert
    assert result is None
