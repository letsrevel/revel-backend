"""Tests for EventQuerySet.for_user() template event exclusion.

Verifies that events with is_template=True are excluded from for_user() results
for all user types (superuser, anonymous, regular authenticated).
"""

import typing as t
from datetime import timedelta

import pytest
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, EventSeries, Organization

pytestmark = pytest.mark.django_db


@pytest.fixture
def _template_and_normal_event(
    organization: Organization,
    event_series: EventSeries,
) -> tuple[Event, Event]:
    """Create a normal event and a template event in the same series.

    Returns:
        A tuple of (normal_event, template_event).
    """
    now = timezone.now()
    normal_event = Event.objects.create(
        organization=organization,
        event_series=event_series,
        name="Normal Event",
        start=now + timedelta(days=7),
        status=Event.EventStatus.OPEN,
        visibility=Event.Visibility.PUBLIC,
        is_template=False,
    )
    template_event = Event.objects.create(
        organization=organization,
        event_series=event_series,
        name="Template Event",
        start=now + timedelta(days=14),
        status=Event.EventStatus.OPEN,
        visibility=Event.Visibility.PUBLIC,
        is_template=True,
    )
    return normal_event, template_event


class TestEventQuerySetTemplateExclusion:
    """Verify that for_user() always excludes template events."""

    def test_for_user_excludes_templates_for_superuser(
        self,
        _template_and_normal_event: tuple[Event, Event],
        django_user_model: t.Type[RevelUser],
    ) -> None:
        """Test that even superusers do not see template events via for_user()."""
        # Arrange
        normal_event, template_event = _template_and_normal_event
        superuser = django_user_model.objects.create_superuser(
            username="su_template_test",
            email="su_template@example.com",
            password="pass",
        )

        # Act
        events = list(Event.objects.for_user(superuser))

        # Assert
        event_ids = [e.id for e in events]
        assert normal_event.id in event_ids
        assert template_event.id not in event_ids

    def test_for_user_excludes_templates_for_anonymous_user(
        self,
        _template_and_normal_event: tuple[Event, Event],
    ) -> None:
        """Test that anonymous users do not see template events via for_user()."""
        # Arrange
        normal_event, template_event = _template_and_normal_event
        anonymous = AnonymousUser()

        # Act
        events = list(Event.objects.for_user(anonymous))

        # Assert
        event_ids = [e.id for e in events]
        assert normal_event.id in event_ids
        assert template_event.id not in event_ids

    def test_for_user_excludes_templates_for_regular_user(
        self,
        _template_and_normal_event: tuple[Event, Event],
        organization_owner_user: RevelUser,
    ) -> None:
        """Test that regular authenticated users do not see template events via for_user()."""
        # Arrange
        normal_event, template_event = _template_and_normal_event

        # Act
        events = list(Event.objects.for_user(organization_owner_user))

        # Assert
        event_ids = [e.id for e in events]
        assert normal_event.id in event_ids
        assert template_event.id not in event_ids

    def test_for_user_with_include_past_still_excludes_templates(
        self,
        _template_and_normal_event: tuple[Event, Event],
        organization_owner_user: RevelUser,
    ) -> None:
        """Test that include_past=True does not accidentally include template events."""
        # Arrange
        normal_event, template_event = _template_and_normal_event

        # Act
        events = list(Event.objects.for_user(organization_owner_user, include_past=True))

        # Assert
        event_ids = [e.id for e in events]
        assert normal_event.id in event_ids
        assert template_event.id not in event_ids

    def test_only_normal_events_returned_when_both_exist(
        self,
        _template_and_normal_event: tuple[Event, Event],
        organization_owner_user: RevelUser,
    ) -> None:
        """Test that for_user() returns only non-template events from the queryset."""
        # Arrange
        normal_event, _ = _template_and_normal_event

        # Act
        events = list(Event.objects.for_user(organization_owner_user))

        # Assert - filter to just events in our org to avoid interference from other tests
        our_events = [e for e in events if e.organization_id == normal_event.organization_id]
        assert all(not e.is_template for e in our_events)
