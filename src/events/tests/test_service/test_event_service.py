"""Tests for the event service."""

from datetime import datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from django.contrib.gis.geos import Point
from django.db.models import QuerySet
from freezegun import freeze_time

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
        event=event, issuer=organization_owner_user, invitation_payload=invitation, ticket_tier_id=vip_tier.id
    )

    assert token.invitation_payload is not None
    assert token.ticket_tier == vip_tier


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
    token = event_service.create_event_token(
        event=event, issuer=organization_owner_user, grants_invitation=True, invitation_payload=invitation_schema
    )

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
    token = event_service.create_event_token(
        event=event, issuer=organization_owner_user, grants_invitation=True, invitation_payload=invitation_schema
    )
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
        event=event, issuer=organization_owner_user, invitation_payload=invitation_schema, max_uses=1
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
    assert event_invitation_request.status == EventInvitationRequest.InvitationRequestStatus.APPROVED
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
    assert event_invitation_request.status == EventInvitationRequest.InvitationRequestStatus.REJECTED
    assert event_invitation_request.decided_by == organization_staff_user


class TestCalculateCalendarDateRange:
    """Tests for calculate_calendar_date_range service function."""

    def test_week_view_with_year(self) -> None:
        """Test week view with explicit year."""
        start, end = event_service.calculate_calendar_date_range(week=1, year=2025)

        # Week 1 of 2025: Jan 4 is Saturday, so Week 1 starts Monday Dec 30, 2024
        assert start == datetime(2024, 12, 30, 0, 0, 0, tzinfo=ZoneInfo("UTC"))
        assert end == datetime(2025, 1, 6, 0, 0, 0, tzinfo=ZoneInfo("UTC"))

    def test_week_view_without_year_uses_current_year(self) -> None:
        """Test week view defaults to current year when year is not provided."""
        with freeze_time("2025-11-27"):
            start, end = event_service.calculate_calendar_date_range(week=48)

            # Week 48 of 2025
            assert start.year == 2025
            assert end.year == 2025
            assert (end - start).days == 7

    def test_week_52_spans_correctly(self) -> None:
        """Test that week 52 calculation works correctly."""
        start, end = event_service.calculate_calendar_date_range(week=52, year=2025)

        assert start.year == 2025
        assert start.month == 12
        assert (end - start).days == 7

    def test_month_view_with_year(self) -> None:
        """Test month view with explicit year."""
        start, end = event_service.calculate_calendar_date_range(month=12, year=2025)

        assert start == datetime(2025, 12, 1, 0, 0, 0, tzinfo=ZoneInfo("UTC"))
        assert end == datetime(2026, 1, 1, 0, 0, 0, tzinfo=ZoneInfo("UTC"))

    def test_month_view_without_year_uses_current_year(self) -> None:
        """Test month view defaults to current year when year is not provided."""
        with freeze_time("2025-11-27"):
            start, end = event_service.calculate_calendar_date_range(month=6)

            assert start == datetime(2025, 6, 1, 0, 0, 0, tzinfo=ZoneInfo("UTC"))
            assert end == datetime(2025, 7, 1, 0, 0, 0, tzinfo=ZoneInfo("UTC"))

    def test_december_month_view_spans_to_next_year(self) -> None:
        """Test that December correctly spans into next year."""
        start, end = event_service.calculate_calendar_date_range(month=12, year=2025)

        assert start == datetime(2025, 12, 1, 0, 0, 0, tzinfo=ZoneInfo("UTC"))
        assert end == datetime(2026, 1, 1, 0, 0, 0, tzinfo=ZoneInfo("UTC"))

    def test_january_month_view(self) -> None:
        """Test January month view."""
        start, end = event_service.calculate_calendar_date_range(month=1, year=2025)

        assert start == datetime(2025, 1, 1, 0, 0, 0, tzinfo=ZoneInfo("UTC"))
        assert end == datetime(2025, 2, 1, 0, 0, 0, tzinfo=ZoneInfo("UTC"))

    def test_year_view(self) -> None:
        """Test year view."""
        start, end = event_service.calculate_calendar_date_range(year=2025)

        assert start == datetime(2025, 1, 1, 0, 0, 0, tzinfo=ZoneInfo("UTC"))
        assert end == datetime(2026, 1, 1, 0, 0, 0, tzinfo=ZoneInfo("UTC"))

    def test_default_to_current_month(self) -> None:
        """Test that no parameters defaults to current month."""
        with freeze_time("2025-11-27"):
            start, end = event_service.calculate_calendar_date_range()

            assert start == datetime(2025, 11, 1, 0, 0, 0, tzinfo=ZoneInfo("UTC"))
            assert end == datetime(2025, 12, 1, 0, 0, 0, tzinfo=ZoneInfo("UTC"))

    def test_default_to_current_month_in_december(self) -> None:
        """Test that default in December spans into next year."""
        with freeze_time("2025-12-25"):
            start, end = event_service.calculate_calendar_date_range()

            assert start == datetime(2025, 12, 1, 0, 0, 0, tzinfo=ZoneInfo("UTC"))
            assert end == datetime(2026, 1, 1, 0, 0, 0, tzinfo=ZoneInfo("UTC"))

    def test_week_takes_priority_over_month(self) -> None:
        """Test that week parameter takes priority over month."""
        start, end = event_service.calculate_calendar_date_range(week=1, month=12, year=2025)

        # Should return Week 1, not December
        assert start.month == 12
        assert start.year == 2024  # Week 1 of 2025 starts in Dec 2024

    def test_week_takes_priority_over_year(self) -> None:
        """Test that week parameter takes priority over bare year."""
        start, end = event_service.calculate_calendar_date_range(week=1, year=2025)

        # Should return Week 1, not entire year
        assert (end - start).days == 7

    def test_month_takes_priority_over_year(self) -> None:
        """Test that month parameter takes priority over bare year."""
        start, end = event_service.calculate_calendar_date_range(month=6, year=2025)

        # Should return June, not entire year
        assert start.month == 6
        assert end.month == 7
