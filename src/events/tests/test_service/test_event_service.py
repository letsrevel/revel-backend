"""Tests for the event service."""

from datetime import datetime, timedelta
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from django.contrib.gis.geos import Point
from django.db.models import QuerySet
from django.utils import timezone
from freezegun import freeze_time

from accounts.models import RevelUser
from events.models import (
    AdditionalResource,
    Event,
    EventInvitation,
    EventInvitationRequest,
    EventToken,
    Organization,
    OrganizationQuestionnaire,
    PotluckItem,
    TicketTier,
)
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


class TestDuplicateEvent:
    """Tests for duplicate_event service function."""

    def test_duplicate_event_basic_fields(self, public_event: Event) -> None:
        """Test that basic fields are copied correctly."""
        new_start = public_event.start + timedelta(days=30)

        new_event = event_service.duplicate_event(
            template_event=public_event,
            new_name="Duplicated Event",
            new_start=new_start,
        )

        assert new_event.id != public_event.id
        assert new_event.name == "Duplicated Event"
        assert new_event.organization == public_event.organization
        assert new_event.event_type == public_event.event_type
        assert new_event.visibility == public_event.visibility
        assert new_event.max_attendees == public_event.max_attendees
        assert new_event.status == Event.EventStatus.DRAFT

    def test_duplicate_event_date_shifting(self, organization: Organization) -> None:
        """Test that date fields are shifted correctly."""
        original_start = timezone.now()
        original_end = original_start + timedelta(hours=3)
        original_rsvp = original_start - timedelta(days=1)
        original_checkin_start = original_start - timedelta(hours=1)
        original_checkin_end = original_end + timedelta(hours=1)

        template = Event.objects.create(
            organization=organization,
            name="Template Event",
            start=original_start,
            end=original_end,
            rsvp_before=original_rsvp,
            check_in_starts_at=original_checkin_start,
            check_in_ends_at=original_checkin_end,
            requires_ticket=False,
        )

        # Shift by 7 days
        new_start = original_start + timedelta(days=7)
        new_event = event_service.duplicate_event(
            template_event=template,
            new_name="Shifted Event",
            new_start=new_start,
        )

        assert new_event.start == new_start
        assert new_event.end == original_end + timedelta(days=7)
        assert new_event.rsvp_before == original_rsvp + timedelta(days=7)
        assert new_event.check_in_starts_at == original_checkin_start + timedelta(days=7)
        assert new_event.check_in_ends_at == original_checkin_end + timedelta(days=7)

    def test_duplicate_event_copies_ticket_tiers(self, public_event: Event) -> None:
        """Test that ticket tiers are duplicated with shifted dates."""
        # Create a tier with dates
        original_tier = TicketTier.objects.create(
            event=public_event,
            name="Early Bird",
            price=25.00,
            total_quantity=50,
            quantity_sold=10,
            sales_start_at=public_event.start - timedelta(days=14),
            sales_end_at=public_event.start - timedelta(days=1),
        )

        new_start = public_event.start + timedelta(days=30)
        new_event = event_service.duplicate_event(
            template_event=public_event,
            new_name="Duplicated Event",
            new_start=new_start,
        )

        new_tiers = list(new_event.ticket_tiers.all())
        # Should have the Early Bird tier (default tier excluded because of signal disconnect)
        early_bird_tier = next((t for t in new_tiers if t.name == "Early Bird"), None)
        assert early_bird_tier is not None
        assert early_bird_tier.price == original_tier.price
        assert early_bird_tier.total_quantity == original_tier.total_quantity
        assert early_bird_tier.quantity_sold == 0  # Reset!
        assert early_bird_tier.sales_start_at == original_tier.sales_start_at + timedelta(days=30)  # type: ignore[operator]
        assert early_bird_tier.sales_end_at == original_tier.sales_end_at + timedelta(days=30)  # type: ignore[operator]

    def test_duplicate_event_copies_suggested_potluck_items(self, organization: Organization) -> None:
        """Test that suggested potluck items are copied but user items are not."""
        template = Event.objects.create(
            organization=organization,
            name="Potluck Event",
            start=timezone.now(),
            potluck_open=True,
            requires_ticket=False,
        )

        # Create a suggested item (host-created)
        PotluckItem.objects.create(
            event=template,
            name="Chips",
            item_type=PotluckItem.ItemTypes.FOOD,
            is_suggested=True,
        )

        # Create a user-contributed item
        PotluckItem.objects.create(
            event=template,
            name="Homemade Cookies",
            item_type=PotluckItem.ItemTypes.DESSERT,
            is_suggested=False,
        )

        new_event = event_service.duplicate_event(
            template_event=template,
            new_name="Duplicated Potluck",
            new_start=template.start + timedelta(days=7),
        )

        new_items = list(new_event.potluck_items.all())
        assert len(new_items) == 1
        assert new_items[0].name == "Chips"
        assert new_items[0].is_suggested is True

    def test_duplicate_event_copies_tags(self, public_event: Event) -> None:
        """Test that tags are copied to the new event."""
        public_event.tags_manager.add("music", "outdoor")

        new_event = event_service.duplicate_event(
            template_event=public_event,
            new_name="Duplicated Event",
            new_start=public_event.start + timedelta(days=30),
        )

        new_tags = [tag.name for tag in new_event.tags_manager.all()]
        assert "music" in new_tags
        assert "outdoor" in new_tags

    def test_duplicate_event_links_questionnaires(
        self, public_event: Event, org_questionnaire: OrganizationQuestionnaire
    ) -> None:
        """Test that the new event is linked to the same questionnaires."""
        org_questionnaire.events.add(public_event)

        new_event = event_service.duplicate_event(
            template_event=public_event,
            new_name="Duplicated Event",
            new_start=public_event.start + timedelta(days=30),
        )

        assert new_event in org_questionnaire.events.all()

    def test_duplicate_event_links_resources(self, public_event: Event) -> None:
        """Test that the new event is linked to the same resources."""
        resource = AdditionalResource.objects.create(
            organization=public_event.organization,
            resource_type=AdditionalResource.ResourceTypes.LINK,
            name="Event Guide",
            link="https://example.com/guide",
        )
        resource.events.add(public_event)

        new_event = event_service.duplicate_event(
            template_event=public_event,
            new_name="Duplicated Event",
            new_start=public_event.start + timedelta(days=30),
        )

        assert new_event in resource.events.all()

    def test_duplicate_event_does_not_copy_user_data(self, public_event: Event, public_user: RevelUser) -> None:
        """Test that user-specific data is not copied."""
        # Create some user-specific data
        EventInvitation.objects.create(event=public_event, user=public_user)

        new_event = event_service.duplicate_event(
            template_event=public_event,
            new_name="Duplicated Event",
            new_start=public_event.start + timedelta(days=30),
        )

        assert new_event.invitations.count() == 0
        assert new_event.tokens.count() == 0

    def test_duplicate_event_negative_delta(self, public_event: Event) -> None:
        """Test that duplicating to an earlier date works correctly."""
        new_start = public_event.start - timedelta(days=7)

        new_event = event_service.duplicate_event(
            template_event=public_event,
            new_name="Earlier Event",
            new_start=new_start,
        )

        assert new_event.start == new_start
        # End should also be shifted backward
        expected_end = public_event.end - timedelta(days=7)
        assert new_event.end == expected_end

    def test_duplicate_event_preserves_event_series(self, event: Event) -> None:
        """Test that event_series reference is preserved."""
        assert event.event_series is not None

        new_event = event_service.duplicate_event(
            template_event=event,
            new_name="Duplicated Series Event",
            new_start=event.start + timedelta(days=30),
        )

        assert new_event.event_series == event.event_series
