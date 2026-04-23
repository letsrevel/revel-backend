"""Tests for ``notify_series_events_generated`` recipient resolution.

These tests pin the visibility-aware audience and notification-preference
filtering of the recurring-event-series digest helper. They are intentionally
narrow: each test sets up exactly the visibility/preference combination it
asserts on so a future regression in the digest's broadcast logic surfaces
immediately and clearly.
"""

import typing as t
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    EventSeries,
    Organization,
    OrganizationFollow,
    OrganizationMember,
    OrganizationStaff,
    RecurrenceRule,
)
from notifications.enums import NotificationType
from notifications.models import NotificationPreference
from notifications.service.notification_helpers import notify_series_events_generated

pytestmark = pytest.mark.django_db


def _build_series(
    organization: Organization,
    *,
    visibility: Event.Visibility,
) -> tuple[EventSeries, Event, Event]:
    """Create a series, template event with the given visibility, and one occurrence."""
    dtstart = timezone.now() + timedelta(days=1)
    rule = RecurrenceRule.objects.create(
        frequency=RecurrenceRule.Frequency.DAILY,
        interval=1,
        dtstart=dtstart,
    )
    series = EventSeries.objects.create(
        organization=organization,
        name=f"Visibility-{visibility} series",
        recurrence_rule=rule,
        is_active=True,
    )
    template = Event.objects.create(
        organization=organization,
        event_series=series,
        name="Template",
        start=dtstart,
        end=dtstart + timedelta(hours=2),
        status=Event.EventStatus.DRAFT,
        visibility=visibility,
        event_type=Event.EventType.PUBLIC,
        is_template=True,
    )
    series.template_event = template
    series.save(update_fields=["template_event"])

    occurrence = Event.objects.create(
        organization=organization,
        event_series=series,
        name="Occurrence",
        start=dtstart + timedelta(days=1),
        end=dtstart + timedelta(days=1, hours=2),
        status=Event.EventStatus.OPEN,
        visibility=visibility,
        event_type=Event.EventType.PUBLIC,
        is_template=False,
    )
    return series, template, occurrence


@pytest.fixture
def revel_user_factory_local(
    django_user_model: type[RevelUser],
) -> t.Callable[..., RevelUser]:
    """Local factory that builds users without trampling NotificationPreference defaults."""

    def _make(username: str) -> RevelUser:
        return django_user_model.objects.create_user(
            username=f"{username}@example.com",
            email=f"{username}@example.com",
            password="strong-password-123!",
        )

    return _make


@pytest.fixture
def organization_with_owner(
    revel_user_factory_local: t.Callable[..., RevelUser],
) -> tuple[Organization, RevelUser]:
    owner = revel_user_factory_local("series-owner")
    org = Organization.objects.create(name="Series Org", slug="series-org", owner=owner)
    return org, owner


def _recipient_user_ids(mock_bulk_create: t.Any) -> set[t.Any]:
    """Extract the unique user IDs the digest tried to notify."""
    if not mock_bulk_create.called:
        return set()
    notifications = mock_bulk_create.call_args[0][0]
    return {n.user.id for n in notifications}


class TestSeriesDigestVisibility:
    """The digest audience must respect ``template_event.visibility``."""

    @patch(
        "notifications.tasks.dispatch_notifications_batch",
        autospec=True,
    )
    @patch(
        "notifications.service.notification_helpers.bulk_create_notifications",
        autospec=True,
    )
    def test_staff_only_visibility_notifies_staff_and_owner(
        self,
        mock_bulk_create: t.Any,
        mock_dispatch: t.Any,
        organization_with_owner: tuple[Organization, RevelUser],
        revel_user_factory_local: t.Callable[..., RevelUser],
    ) -> None:
        """STAFF_ONLY: only owner and staff. Members and followers must be excluded."""
        # Arrange
        org, owner = organization_with_owner
        staff = revel_user_factory_local("staff")
        OrganizationStaff.objects.create(user=staff, organization=org)
        member_user = revel_user_factory_local("member")
        OrganizationMember.objects.create(
            user=member_user,
            organization=org,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        follower = revel_user_factory_local("follower")
        OrganizationFollow.objects.create(user=follower, organization=org, notify_new_events=True)

        mock_bulk_create.return_value = []

        series, _, occurrence = _build_series(org, visibility=Event.Visibility.STAFF_ONLY)

        # Act
        notify_series_events_generated(series, [occurrence])

        # Assert
        recipient_ids = _recipient_user_ids(mock_bulk_create)
        assert owner.id in recipient_ids
        assert staff.id in recipient_ids
        assert member_user.id not in recipient_ids
        assert follower.id not in recipient_ids

    @patch(
        "notifications.tasks.dispatch_notifications_batch",
        autospec=True,
    )
    @patch(
        "notifications.service.notification_helpers.bulk_create_notifications",
        autospec=True,
    )
    def test_members_only_visibility_notifies_members_no_followers(
        self,
        mock_bulk_create: t.Any,
        mock_dispatch: t.Any,
        organization_with_owner: tuple[Organization, RevelUser],
        revel_user_factory_local: t.Callable[..., RevelUser],
    ) -> None:
        """MEMBERS_ONLY: owner, staff, active/paused members. Followers excluded."""
        # Arrange
        org, owner = organization_with_owner
        active_member = revel_user_factory_local("active")
        OrganizationMember.objects.create(
            user=active_member,
            organization=org,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        paused_member = revel_user_factory_local("paused")
        OrganizationMember.objects.create(
            user=paused_member,
            organization=org,
            status=OrganizationMember.MembershipStatus.PAUSED,
        )
        cancelled_member = revel_user_factory_local("cancelled")
        OrganizationMember.objects.create(
            user=cancelled_member,
            organization=org,
            status=OrganizationMember.MembershipStatus.CANCELLED,
        )
        follower = revel_user_factory_local("follower-mo")
        OrganizationFollow.objects.create(user=follower, organization=org, notify_new_events=True)

        mock_bulk_create.return_value = []

        series, _, occurrence = _build_series(org, visibility=Event.Visibility.MEMBERS_ONLY)

        # Act
        notify_series_events_generated(series, [occurrence])

        # Assert
        recipient_ids = _recipient_user_ids(mock_bulk_create)
        assert owner.id in recipient_ids
        assert active_member.id in recipient_ids
        assert paused_member.id in recipient_ids
        assert cancelled_member.id not in recipient_ids
        assert follower.id not in recipient_ids

    @patch(
        "notifications.tasks.dispatch_notifications_batch",
        autospec=True,
    )
    @patch(
        "notifications.service.notification_helpers.bulk_create_notifications",
        autospec=True,
    )
    def test_private_visibility_notifies_only_staff_and_owner(
        self,
        mock_bulk_create: t.Any,
        mock_dispatch: t.Any,
        organization_with_owner: tuple[Organization, RevelUser],
        revel_user_factory_local: t.Callable[..., RevelUser],
    ) -> None:
        """PRIVATE: explicitly-shared events; broadcast only to staff and owner."""
        # Arrange
        org, owner = organization_with_owner
        member_user = revel_user_factory_local("member-priv")
        OrganizationMember.objects.create(
            user=member_user,
            organization=org,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        follower = revel_user_factory_local("follower-priv")
        OrganizationFollow.objects.create(user=follower, organization=org, notify_new_events=True)

        mock_bulk_create.return_value = []

        series, _, occurrence = _build_series(org, visibility=Event.Visibility.PRIVATE)

        # Act
        notify_series_events_generated(series, [occurrence])

        # Assert
        recipient_ids = _recipient_user_ids(mock_bulk_create)
        assert owner.id in recipient_ids
        assert member_user.id not in recipient_ids
        assert follower.id not in recipient_ids

    @patch(
        "notifications.tasks.dispatch_notifications_batch",
        autospec=True,
    )
    @patch(
        "notifications.service.notification_helpers.bulk_create_notifications",
        autospec=True,
    )
    def test_unlisted_visibility_notifies_only_staff_and_owner(
        self,
        mock_bulk_create: t.Any,
        mock_dispatch: t.Any,
        organization_with_owner: tuple[Organization, RevelUser],
        revel_user_factory_local: t.Callable[..., RevelUser],
    ) -> None:
        """UNLISTED: hidden from discovery, must not be broadcast to members or followers."""
        # Arrange
        org, owner = organization_with_owner
        member_user = revel_user_factory_local("member-unl")
        OrganizationMember.objects.create(
            user=member_user,
            organization=org,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        follower = revel_user_factory_local("follower-unl")
        OrganizationFollow.objects.create(user=follower, organization=org, notify_new_events=True)

        mock_bulk_create.return_value = []

        series, _, occurrence = _build_series(org, visibility=Event.Visibility.UNLISTED)

        # Act
        notify_series_events_generated(series, [occurrence])

        # Assert
        recipient_ids = _recipient_user_ids(mock_bulk_create)
        assert owner.id in recipient_ids
        assert member_user.id not in recipient_ids
        assert follower.id not in recipient_ids

    @patch(
        "notifications.tasks.dispatch_notifications_batch",
        autospec=True,
    )
    @patch(
        "notifications.service.notification_helpers.bulk_create_notifications",
        autospec=True,
    )
    def test_public_visibility_notifies_everyone(
        self,
        mock_bulk_create: t.Any,
        mock_dispatch: t.Any,
        organization_with_owner: tuple[Organization, RevelUser],
        revel_user_factory_local: t.Callable[..., RevelUser],
    ) -> None:
        """PUBLIC: owner, staff, active/paused members, and opted-in followers."""
        # Arrange
        org, owner = organization_with_owner
        staff = revel_user_factory_local("staff-pub")
        OrganizationStaff.objects.create(user=staff, organization=org)
        member_user = revel_user_factory_local("member-pub")
        OrganizationMember.objects.create(
            user=member_user,
            organization=org,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        follower = revel_user_factory_local("follower-pub")
        OrganizationFollow.objects.create(user=follower, organization=org, notify_new_events=True)

        mock_bulk_create.return_value = []

        series, _, occurrence = _build_series(org, visibility=Event.Visibility.PUBLIC)

        # Act
        notify_series_events_generated(series, [occurrence])

        # Assert
        recipient_ids = _recipient_user_ids(mock_bulk_create)
        assert owner.id in recipient_ids
        assert staff.id in recipient_ids
        assert member_user.id in recipient_ids
        assert follower.id in recipient_ids


class TestSeriesDigestPreferences:
    """The digest must honour user notification preferences."""

    @patch(
        "notifications.tasks.dispatch_notifications_batch",
        autospec=True,
    )
    @patch(
        "notifications.service.notification_helpers.bulk_create_notifications",
        autospec=True,
    )
    def test_silence_all_excludes_user(
        self,
        mock_bulk_create: t.Any,
        mock_dispatch: t.Any,
        organization_with_owner: tuple[Organization, RevelUser],
        revel_user_factory_local: t.Callable[..., RevelUser],
    ) -> None:
        """A staff member who silenced all notifications must not receive the digest."""
        # Arrange
        org, owner = organization_with_owner
        silenced_staff = revel_user_factory_local("silenced")
        OrganizationStaff.objects.create(user=silenced_staff, organization=org)
        NotificationPreference.objects.update_or_create(
            user=silenced_staff,
            defaults={"silence_all_notifications": True},
        )

        mock_bulk_create.return_value = []

        series, _, occurrence = _build_series(org, visibility=Event.Visibility.PUBLIC)

        # Act
        notify_series_events_generated(series, [occurrence])

        # Assert
        recipient_ids = _recipient_user_ids(mock_bulk_create)
        assert owner.id in recipient_ids
        assert silenced_staff.id not in recipient_ids

    @patch(
        "notifications.tasks.dispatch_notifications_batch",
        autospec=True,
    )
    @patch(
        "notifications.service.notification_helpers.bulk_create_notifications",
        autospec=True,
    )
    def test_disabled_series_events_generated_excludes_user(
        self,
        mock_bulk_create: t.Any,
        mock_dispatch: t.Any,
        organization_with_owner: tuple[Organization, RevelUser],
        revel_user_factory_local: t.Callable[..., RevelUser],
    ) -> None:
        """A user who specifically disabled SERIES_EVENTS_GENERATED is excluded."""
        # Arrange
        org, owner = organization_with_owner
        opted_out = revel_user_factory_local("opted-out")
        OrganizationMember.objects.create(
            user=opted_out,
            organization=org,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
        prefs, _created = NotificationPreference.objects.get_or_create(user=opted_out)
        settings = dict(prefs.notification_type_settings or {})
        settings[NotificationType.SERIES_EVENTS_GENERATED] = {"enabled": False}
        prefs.notification_type_settings = settings
        prefs.save(update_fields=["notification_type_settings"])

        mock_bulk_create.return_value = []

        series, _, occurrence = _build_series(org, visibility=Event.Visibility.PUBLIC)

        # Act
        notify_series_events_generated(series, [occurrence])

        # Assert
        recipient_ids = _recipient_user_ids(mock_bulk_create)
        assert owner.id in recipient_ids
        assert opted_out.id not in recipient_ids
