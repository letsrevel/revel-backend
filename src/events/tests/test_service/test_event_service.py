"""Tests for the event service."""

from unittest.mock import Mock

import pytest
from django.contrib.gis.geos import Point
from django.db.models import QuerySet

from accounts.models import RevelUser
from events.models import Event, EventInvitation, EventInvitationRequest, EventToken, TicketTier
from events.models.mixins import LocationMixin
from events.schema import InvitationBaseSchema
from events.service import event_service

pytestmark = pytest.mark.django_db


def test_order_by_distance_with_point() -> None:
    """Test that the queryset is annotated and ordered when a point is provided."""
    # Arrange
    point = Point(1, 1, srid=4326)
    queryset: Mock = Mock(spec=["annotate", "order_by"])
    annotated_queryset: Mock = Mock(spec=["order_by"])
    queryset.annotate.return_value = annotated_queryset

    # Act
    result_queryset: "QuerySet[LocationMixin]" = event_service.order_by_distance(point, queryset)

    # Assert
    queryset.annotate.assert_called_once()
    annotated_queryset.order_by.assert_called_once_with("distance")
    assert result_queryset == annotated_queryset.order_by.return_value


def test_order_by_distance_with_none_point() -> None:
    """Test that the queryset is returned as is when point is None."""
    # Arrange
    point = None
    queryset: Mock = Mock(spec=["annotate", "order_by"])

    # Act
    result_queryset: "QuerySet[LocationMixin]" = event_service.order_by_distance(point, queryset)

    # Assert
    queryset.annotate.assert_not_called()
    queryset.order_by.assert_not_called()
    assert result_queryset == queryset


def test_create_event_token_creates_token(event: Event, organization_owner_user: RevelUser) -> None:
    """Test that the function creates an EventToken."""
    # Act
    token = event_service.create_event_token(event=event, issuer=organization_owner_user)

    # Assert
    assert EventToken.objects.filter(id=token.id).exists()


def test_create_event_token_with_invitation(
    event: Event, organization_owner_user: RevelUser, vip_tier: TicketTier
) -> None:
    """Test that the function creates an EventToken with an invitation."""
    # Arrange
    invitation = InvitationBaseSchema(waives_questionnaire=True, overrides_max_attendees=False)

    # Act
    token = event_service.create_event_token(
        event=event, issuer=organization_owner_user, invitation=invitation, invitation_tier_id=vip_tier.id
    )

    assert token.invitation_payload is not None
    assert token.invitation_tier == vip_tier


def test_get_event_token_returns_token(event: Event, organization_owner_user: RevelUser) -> None:
    """Test that the function returns an EventToken."""
    # Arrange
    event_service.create_event_token(event=event, issuer=organization_owner_user)


def test_get_event_token_returns_none_for_invalid_token() -> None:
    """Test that the function returns None for an invalid token."""
    # Act
    token = event_service.get_event_token("invalid-token")

    # Assert
    assert token is None


def test_claim_invitation_returns_invitation(
    event: Event, organization_owner_user: RevelUser, public_user: RevelUser
) -> None:
    """Test that the function returns an EventInvitation."""
    # Arrange
    invitation_schema = InvitationBaseSchema(waives_questionnaire=True, overrides_max_attendees=False)
    token = event_service.create_event_token(event=event, issuer=organization_owner_user, invitation=invitation_schema)

    # Act
    invitation = event_service.claim_invitation(public_user, token.id)

    # Assert
    assert invitation is not None
    assert invitation.user == public_user
    assert invitation.event == event


def test_claim_invitation_increments_uses(
    event: Event, organization_owner_user: RevelUser, public_user: RevelUser
) -> None:
    """Test that the function increments the uses count."""
    # Arrange
    invitation_schema = InvitationBaseSchema(waives_questionnaire=True, overrides_max_attendees=False)
    token = event_service.create_event_token(event=event, issuer=organization_owner_user, invitation=invitation_schema)
    assert token.uses == 0

    # Act
    event_service.claim_invitation(public_user, token.id)

    # Assert
    token.refresh_from_db()
    assert token.uses == 1


def test_claim_invitation_with_max_uses(
    event: Event, organization_owner_user: RevelUser, public_user: RevelUser
) -> None:
    """Test that the function returns None when max_uses is reached."""
    # Arrange
    invitation_schema = InvitationBaseSchema(waives_questionnaire=True, overrides_max_attendees=False)
    token = event_service.create_event_token(
        event=event, issuer=organization_owner_user, invitation=invitation_schema, max_uses=1
    )
    event_service.claim_invitation(public_user, token.id)

    # Act
    invitation = event_service.claim_invitation(public_user, token.id)

    # Assert
    assert invitation is None


def test_approve_invitation_request_creates_invitation(
    event_invitation_request: EventInvitationRequest, organization_staff_user: RevelUser
) -> None:
    """Test that an invitation is created when a request is approved."""
    # Arrange
    assert not EventInvitation.objects.filter(
        event=event_invitation_request.event, user=event_invitation_request.user
    ).exists()

    # Act
    event_service.approve_invitation_request(event_invitation_request, organization_staff_user)

    # Assert
    assert EventInvitation.objects.filter(
        event=event_invitation_request.event, user=event_invitation_request.user
    ).exists()
    assert event_invitation_request.status == EventInvitationRequest.Status.APPROVED
    assert event_invitation_request.decided_by == organization_staff_user


def test_reject_invitation_request_does_not_create_invitation(
    event_invitation_request: EventInvitationRequest, organization_staff_user: RevelUser
) -> None:
    """Test that an invitation is not created when a request is rejected."""
    # Arrange
    assert not EventInvitation.objects.filter(
        event=event_invitation_request.event, user=event_invitation_request.user
    ).exists()

    # Act
    event_service.reject_invitation_request(event_invitation_request, organization_staff_user)

    # Assert
    assert not EventInvitation.objects.filter(
        event=event_invitation_request.event, user=event_invitation_request.user
    ).exists()
    assert event_invitation_request.status == EventInvitationRequest.Status.REJECTED
    assert event_invitation_request.decided_by == organization_staff_user
