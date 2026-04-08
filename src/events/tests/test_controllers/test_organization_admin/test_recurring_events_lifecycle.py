"""Tests for the recurring events lifecycle endpoints (cancel/generate/pause/resume).

Split from the original ``test_recurring_events.py`` to keep individual test
files under the project's 1000-line limit. Sibling files cover create and
template/recurrence updates. Shared helpers live in
``_recurring_events_helpers.py``.
"""

import typing as t
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest.mock import patch
from uuid import uuid4

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from events.models import Organization

from ._recurring_events_helpers import _make_series_with_rule

pytestmark = pytest.mark.django_db


class TestCancelOccurrence:
    """Tests for the cancel-occurrence endpoint."""

    def test_cancel_occurrence_adds_exdate(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that cancelling an occurrence adds the date to exdates.

        Asserts both the count AND the value — the stored exdate must be the
        UTC-normalized ISO 8601 representation of the cancelled instant, so
        equivalent instants sent in different timezones collapse to one entry.
        """
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
        expected = occurrence_date.astimezone(dt_timezone.utc).isoformat()
        assert data["exdates"][0] == expected

    def test_cancel_occurrence_returns_404_for_nonmember(
        self,
        nonmember_client: Client,
        organization: Organization,
    ) -> None:
        """A non-member gets 404 (not 403) because the org is not discoverable.

        Returning 404 avoids leaking org existence to unauthorised users; the
        endpoint is effectively invisible outside the org membership.
        """
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
        """Test that providing an ``until`` parameter limits generation.

        Controller-level test: we verify the endpoint accepts the override and
        returns a capped list. Exact-date assertions for the rolling-window
        boundary live in the service-level tests
        (``test_recurrence_service_materialize.py::test_with_until_override``)
        where they can use ``freeze_time`` safely. Using ``freeze_time`` here
        would both invalidate the JWT (issued at wall time, verified under
        frozen time) AND break pydantic schema generation for Django's lazily-
        built URL resolver.
        """
        # Arrange — daily rule; cap at dtstart + 2 days, 12h so exactly 3
        # daily occurrences fit in (dtstart-1s, until): day 0, +1d, +2d.
        series = _make_series_with_rule(organization)
        assert series.recurrence_rule is not None
        dtstart = series.recurrence_rule.dtstart
        until = dtstart + timedelta(days=2, hours=12)
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

        # Assert — exactly 3 daily occurrences inside the override window.
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 3
        starts = sorted(datetime.fromisoformat(item["start"]) for item in data)
        for event_start in starts:
            assert event_start <= until


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

    def test_pause_returns_404_for_nonmember(
        self,
        nonmember_client: Client,
        organization: Organization,
    ) -> None:
        """A non-member gets 404 (not 403) because the org is not discoverable.

        Returning 404 avoids leaking org existence to unauthorised users; the
        endpoint is effectively invisible outside the org membership.
        """
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
