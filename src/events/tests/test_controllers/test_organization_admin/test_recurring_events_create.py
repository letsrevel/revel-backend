"""Tests for the create-recurring-event controller endpoint.

Split from the original ``test_recurring_events.py`` to keep individual test
files under the project's 1000-line limit. Sibling files cover lifecycle
operations (cancel/generate/pause/resume) and template/recurrence updates.
Shared helpers live in ``_recurring_events_helpers.py``.
"""

import typing as t
from unittest.mock import patch

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import (
    EventSeries,
    Organization,
    OrganizationStaff,
)

from ._recurring_events_helpers import _create_recurring_event_payload

pytestmark = pytest.mark.django_db


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
        assert response.status_code == 201
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
        assert response.status_code == 201
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
        assert response.status_code == 201

    def test_rejects_zero_generation_window(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Schema must reject generation_window_weeks=0 (model requires >= 1)."""
        # Arrange
        url = reverse("api:create_recurring_event", kwargs={"slug": organization.slug})
        payload = _create_recurring_event_payload()
        payload["generation_window_weeks"] = 0

        # Act
        response = organization_owner_client.post(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 422

    def test_rejects_excessively_large_generation_window(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Schema must cap generation_window_weeks to keep the rolling window bounded."""
        # Arrange
        url = reverse("api:create_recurring_event", kwargs={"slug": organization.slug})
        payload = _create_recurring_event_payload()
        payload["generation_window_weeks"] = 53  # one above the cap

        # Act
        response = organization_owner_client.post(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 422
