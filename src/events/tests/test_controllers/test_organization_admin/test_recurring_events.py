"""Tests for the recurring events controller endpoints.

Tests cover creation, cancellation, manual generation, pause/resume,
and recurrence updates via the OrganizationAdminRecurringEventsController.
"""

import typing as t
from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    EventSeries,
    Organization,
    OrganizationStaff,
    RecurrenceRule,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def staff_with_create_event_permission(
    organization: Organization,
    organization_staff_user: RevelUser,
    staff_member: OrganizationStaff,
) -> OrganizationStaff:
    """Grant the staff member create_event permission."""
    perms = staff_member.permissions
    perms["default"]["create_event"] = True
    staff_member.permissions = perms
    staff_member.save()
    return staff_member


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_recurring_event_payload(start: t.Any = None) -> dict[str, t.Any]:
    """Build a valid payload for the create-recurring-event endpoint."""
    if start is None:
        start_dt = timezone.now() + timedelta(days=1)
    else:
        start_dt = start
    end_dt = start_dt + timedelta(hours=2)

    return {
        "event": {
            "name": "Weekly Standup",
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "event_type": "public",
            "visibility": "public",
        },
        "series_name": "Weekly Standup Series",
        "series_description": "Our weekly standup meeting",
        "recurrence": {
            "frequency": "weekly",
            "interval": 1,
            "weekdays": [0],
            "dtstart": start_dt.isoformat(),
            "timezone": "UTC",
        },
        "auto_publish": False,
        "generation_window_weeks": 4,
    }


def _make_series_with_rule(organization: Organization) -> EventSeries:
    """Create an EventSeries with recurrence rule, template event, and correct FK links.

    Ensures the template event's event_series FK points to the series that owns it,
    so that duplicate_event() copies the correct FK to materialized events.
    """
    dtstart = timezone.make_aware(datetime(2026, 4, 6, 10, 0))
    rule = RecurrenceRule.objects.create(
        frequency=RecurrenceRule.Frequency.WEEKLY,
        interval=1,
        weekdays=[0],
        dtstart=dtstart,
    )
    series = EventSeries.objects.create(
        organization=organization,
        name="Test Recurring Series",
        recurrence_rule=rule,
        is_active=True,
        auto_publish=False,
        generation_window_weeks=4,
    )
    template_event = Event.objects.create(
        organization=organization,
        event_series=series,
        name="Controller Template",
        start=dtstart,
        end=dtstart + timedelta(hours=2),
        status=Event.EventStatus.DRAFT,
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        is_template=True,
    )
    series.template_event = template_event
    series.save(update_fields=["template_event"])
    return series


# ---------------------------------------------------------------------------
# POST /create-recurring-event
# ---------------------------------------------------------------------------


class TestCreateRecurringEvent:
    """Tests for the create-recurring-event endpoint."""

    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_owner_can_create_recurring_event(
        self,
        mock_notify: t.Any,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that the organization owner can create a recurring event series."""
        # Arrange
        url = reverse("api:create_recurring_event", kwargs={"slug": organization.slug})
        payload = _create_recurring_event_payload()

        # Act
        response = organization_owner_client.post(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Weekly Standup Series"
        assert data["is_active"] is True
        assert data["auto_publish"] is False
        assert data["generation_window_weeks"] == 4
        assert data["recurrence_rule"] is not None
        assert data["recurrence_rule"]["frequency"] == "weekly"
        assert data["template_event"] is not None

    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_creates_series_rule_template_and_events(
        self,
        mock_notify: t.Any,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that creating a recurring event creates all related objects in the DB."""
        # Arrange
        url = reverse("api:create_recurring_event", kwargs={"slug": organization.slug})
        payload = _create_recurring_event_payload()

        # Act
        response = organization_owner_client.post(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        series_id = data["id"]

        series = EventSeries.objects.get(id=series_id)
        assert series.recurrence_rule is not None
        assert series.template_event is not None
        assert series.template_event.is_template is True

        # Check that some materialized events were generated
        materialized_events = series.events.filter(is_template=False)
        assert materialized_events.count() > 0

    def test_nonmember_user_gets_404(
        self,
        nonmember_client: Client,
        organization: Organization,
    ) -> None:
        """Test that a non-member user gets 404 (org hidden from non-members)."""
        # Arrange
        url = reverse("api:create_recurring_event", kwargs={"slug": organization.slug})
        payload = _create_recurring_event_payload()

        # Act
        response = nonmember_client.post(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 404

    def test_member_without_permission_gets_403(
        self,
        member_client: Client,
        organization: Organization,
    ) -> None:
        """Test that a regular member without create_event permission gets 403."""
        # Arrange
        url = reverse("api:create_recurring_event", kwargs={"slug": organization.slug})
        payload = _create_recurring_event_payload()

        # Act
        response = member_client.post(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 403

    def test_unauthenticated_gets_401(
        self,
        client: Client,
        organization: Organization,
    ) -> None:
        """Test that an unauthenticated request gets 401."""
        # Arrange
        url = reverse("api:create_recurring_event", kwargs={"slug": organization.slug})
        payload = _create_recurring_event_payload()

        # Act
        response = client.post(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 401

    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_staff_with_create_event_permission_can_create(
        self,
        mock_notify: t.Any,
        organization_staff_client: Client,
        organization: Organization,
        staff_with_create_event_permission: OrganizationStaff,
    ) -> None:
        """Test that staff with create_event permission can create recurring events."""
        # Arrange
        url = reverse("api:create_recurring_event", kwargs={"slug": organization.slug})
        payload = _create_recurring_event_payload()

        # Act
        response = organization_staff_client.post(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# POST /event-series/{id}/cancel-occurrence
# ---------------------------------------------------------------------------


class TestCancelOccurrence:
    """Tests for the cancel-occurrence endpoint."""

    def test_cancel_occurrence_adds_exdate(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that cancelling an occurrence adds the date to exdates."""
        # Arrange
        series = _make_series_with_rule(organization)
        occurrence_date = timezone.make_aware(datetime(2026, 4, 13, 10, 0))
        url = reverse(
            "api:cancel_series_occurrence",
            kwargs={"slug": organization.slug, "series_id": str(series.id)},
        )
        payload = {"occurrence_date": occurrence_date.isoformat()}

        # Act
        response = organization_owner_client.post(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data["exdates"]) == 1

    def test_cancel_occurrence_permission_denied(
        self,
        nonmember_client: Client,
        organization: Organization,
    ) -> None:
        """Test that a non-member user cannot cancel an occurrence (org hidden)."""
        # Arrange
        url = reverse(
            "api:cancel_series_occurrence",
            kwargs={"slug": organization.slug, "series_id": str(uuid4())},
        )
        payload = {"occurrence_date": timezone.now().isoformat()}

        # Act
        response = nonmember_client.post(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /event-series/{id}/generate
# ---------------------------------------------------------------------------


class TestGenerateEvents:
    """Tests for the manual event generation endpoint."""

    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_generate_events_returns_list(
        self,
        mock_notify: t.Any,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that manual generation returns a list of created events."""
        # Arrange
        series = _make_series_with_rule(organization)
        url = reverse(
            "api:generate_series_events",
            kwargs={"slug": organization.slug, "series_id": str(series.id)},
        )

        # Act
        response = organization_owner_client.post(
            url,
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) > 0

    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_generate_with_until_override(
        self,
        mock_notify: t.Any,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that providing an 'until' parameter limits generation."""
        # Arrange
        series = _make_series_with_rule(organization)
        until = timezone.make_aware(datetime(2026, 4, 20, 23, 59))
        url = reverse(
            "api:generate_series_events",
            kwargs={"slug": organization.slug, "series_id": str(series.id)},
        )
        payload = {"until": until.isoformat()}

        # Act
        response = organization_owner_client.post(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)


# ---------------------------------------------------------------------------
# POST /event-series/{id}/pause and /resume
# ---------------------------------------------------------------------------


class TestPauseResumeSeries:
    """Tests for the pause and resume endpoints."""

    def test_pause_series_sets_inactive(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that pausing a series sets is_active to False."""
        # Arrange
        series = _make_series_with_rule(organization)
        assert series.is_active is True
        url = reverse(
            "api:pause_series",
            kwargs={"slug": organization.slug, "series_id": str(series.id)},
        )

        # Act
        response = organization_owner_client.post(
            url,
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["is_active"] is False

    def test_resume_series_sets_active(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that resuming a series sets is_active to True."""
        # Arrange
        series = _make_series_with_rule(organization)
        series.is_active = False
        series.save(update_fields=["is_active"])

        url = reverse(
            "api:resume_series",
            kwargs={"slug": organization.slug, "series_id": str(series.id)},
        )

        # Act
        response = organization_owner_client.post(
            url,
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["is_active"] is True

    def test_pause_permission_denied_for_nonmember(
        self,
        nonmember_client: Client,
        organization: Organization,
    ) -> None:
        """Test that a non-member cannot pause a series (org hidden)."""
        # Arrange
        url = reverse(
            "api:pause_series",
            kwargs={"slug": organization.slug, "series_id": str(uuid4())},
        )

        # Act
        response = nonmember_client.post(
            url,
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /event-series/{id}/recurrence
# ---------------------------------------------------------------------------


class TestUpdateRecurrence:
    """Tests for the recurrence update endpoint."""

    def test_update_auto_publish(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that auto_publish can be updated via PATCH."""
        # Arrange
        series = _make_series_with_rule(organization)
        assert series.auto_publish is False
        url = reverse(
            "api:update_series_recurrence",
            kwargs={"slug": organization.slug, "series_id": str(series.id)},
        )
        payload = {"auto_publish": True}

        # Act
        response = organization_owner_client.patch(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["auto_publish"] is True

    def test_update_generation_window_weeks(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that generation_window_weeks can be updated via PATCH."""
        # Arrange
        series = _make_series_with_rule(organization)
        url = reverse(
            "api:update_series_recurrence",
            kwargs={"slug": organization.slug, "series_id": str(series.id)},
        )
        payload = {"generation_window_weeks": 12}

        # Act
        response = organization_owner_client.patch(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["generation_window_weeks"] == 12

    def test_update_recurrence_rule_interval(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that the recurrence rule's interval can be updated via nested payload."""
        # Arrange
        series = _make_series_with_rule(organization)
        url = reverse(
            "api:update_series_recurrence",
            kwargs={"slug": organization.slug, "series_id": str(series.id)},
        )
        payload = {"recurrence": {"interval": 2}}

        # Act
        response = organization_owner_client.patch(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["recurrence_rule"]["interval"] == 2

    def test_update_nonexistent_series_returns_404(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that updating a non-existent series returns 404."""
        # Arrange
        url = reverse(
            "api:update_series_recurrence",
            kwargs={"slug": organization.slug, "series_id": str(uuid4())},
        )
        payload = {"auto_publish": True}

        # Act
        response = organization_owner_client.patch(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 404

    def test_update_permission_denied_for_nonmember(
        self,
        nonmember_client: Client,
        organization: Organization,
    ) -> None:
        """Test that a non-member cannot update recurrence settings (org hidden)."""
        # Arrange
        url = reverse(
            "api:update_series_recurrence",
            kwargs={"slug": organization.slug, "series_id": str(uuid4())},
        )
        payload = {"auto_publish": True}

        # Act
        response = nonmember_client.patch(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 404
