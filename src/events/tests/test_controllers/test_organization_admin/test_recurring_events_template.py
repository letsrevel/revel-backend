"""Tests for the template/recurrence update endpoints.

Split from the original ``test_recurring_events.py`` to keep individual test
files under the project's 1000-line limit. Sibling files cover create and
lifecycle operations. Shared helpers live in ``_recurring_events_helpers.py``.
"""

from unittest.mock import patch
from uuid import uuid4

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import Organization

from ._recurring_events_helpers import (
    _make_series_with_future_dtstart,
    _make_series_with_rule,
)

pytestmark = pytest.mark.django_db


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

    def test_update_recurrence_returns_404_for_nonmember(
        self,
        nonmember_client: Client,
        organization: Organization,
    ) -> None:
        """A non-member gets 404 (not 403) because the org is not discoverable.

        Using a *real* series id ensures we're testing the authorisation
        branch — if the controller accidentally exposed an existing series
        to unauthorised callers, this test would start failing instead of
        silently passing on a "resource does not exist" 404.
        """
        # Arrange
        series = _make_series_with_rule(organization)
        url = reverse(
            "api:update_series_recurrence",
            kwargs={"slug": organization.slug, "series_id": str(series.id)},
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


class TestUpdateTemplate:
    """Tests for the template update endpoint with propagation."""

    def test_update_template_without_propagation(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Updating the template alone (propagate=none) only changes the template."""
        # Arrange — use a series whose dtstart is in the near future so
        # generate_series_events (without freeze_time, to keep JWT valid) still
        # yields events.
        from events.service import recurrence_service

        series = _make_series_with_future_dtstart(organization)
        with patch("notifications.service.notification_helpers.notify_series_events_generated"):
            created = recurrence_service.generate_series_events(series)
        assert len(created) > 0
        original_descriptions = [e.description for e in created]

        url = reverse(
            "api:update_series_template",
            kwargs={"slug": organization.slug, "series_id": str(series.id)},
        )
        payload = {"description": "Updated template description"}

        # Act
        response = organization_owner_client.patch(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert — template updated, occurrences untouched
        assert response.status_code == 200
        series.refresh_from_db()
        assert series.template_event is not None
        assert series.template_event.description == "Updated template description"
        for event, original in zip(created, original_descriptions, strict=True):
            event.refresh_from_db()
            assert event.description == original

    def test_update_template_with_future_unmodified_propagation(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """propagate=future_unmodified updates only future events not marked as modified."""
        # Arrange — use a future-dtstart series so all materialized occurrences
        # are in the future relative to wall time (needed because propagate
        # filters on ``start__gte=timezone.now()`` and we don't freeze time).
        from events.service import recurrence_service

        series = _make_series_with_future_dtstart(organization)
        with patch("notifications.service.notification_helpers.notify_series_events_generated"):
            created = recurrence_service.generate_series_events(series)
        assert len(created) >= 2

        # Mark one as modified — it should NOT be updated.
        modified_event = created[0]
        modified_event.is_modified = True
        modified_event.save(update_fields=["is_modified"])
        unmodified_event = created[1]

        url = reverse(
            "api:update_series_template",
            kwargs={"slug": organization.slug, "series_id": str(series.id)},
        )
        payload = {"description": "Propagated description"}

        # Act
        response = organization_owner_client.patch(
            f"{url}?propagate=future_unmodified",
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 200
        modified_event.refresh_from_db()
        unmodified_event.refresh_from_db()
        assert modified_event.description != "Propagated description"
        assert unmodified_event.description == "Propagated description"

    def test_update_template_with_invalid_propagate_returns_422(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Invalid propagate values are rejected by the PropagateScope enum at the API boundary."""
        # Arrange
        series = _make_series_with_rule(organization)
        assert series.template_event is not None
        original_description = series.template_event.description
        url = reverse(
            "api:update_series_template",
            kwargs={"slug": organization.slug, "series_id": str(series.id)},
        )
        payload = {"description": "x"}

        # Act
        response = organization_owner_client.patch(
            f"{url}?propagate=bogus_scope",
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert — schema rejection must not have applied any template changes.
        assert response.status_code == 422
        series.template_event.refresh_from_db()
        assert series.template_event.description == original_description

    def test_update_template_returns_404_for_nonmember(
        self,
        nonmember_client: Client,
        organization: Organization,
    ) -> None:
        """A non-member gets 404 (not 403) because the org is not discoverable.

        Using a real series id ensures we're exercising the authorisation
        path, not the "resource does not exist" branch.
        """
        # Arrange
        series = _make_series_with_rule(organization)
        url = reverse(
            "api:update_series_template",
            kwargs={"slug": organization.slug, "series_id": str(series.id)},
        )
        payload = {"description": "x"}

        # Act
        response = nonmember_client.patch(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert
        assert response.status_code == 404

    def test_update_template_rejects_event_series_id(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """The template edit payload must not allow rebinding the template to another series.

        ``TemplateEditSchema`` deliberately omits ``event_series_id`` from the
        editable field set and declares ``extra="forbid"``, so sending it
        must return 422. This regression test guards against reintroducing
        ``EventEditSchema`` which exposes that FK.
        """
        # Arrange — two separate series in the same org.
        series = _make_series_with_rule(organization)
        other_series = _make_series_with_rule(organization)
        assert series.template_event is not None
        original_series_id = series.template_event.event_series_id

        url = reverse(
            "api:update_series_template",
            kwargs={"slug": organization.slug, "series_id": str(series.id)},
        )
        payload = {"event_series_id": str(other_series.id)}

        # Act
        response = organization_owner_client.patch(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert — deterministic 422 from ``extra="forbid"``.
        assert response.status_code == 422
        series.refresh_from_db()
        assert series.template_event is not None
        assert series.template_event.event_series_id == original_series_id

    def test_update_template_rejects_venue_id(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """The template edit payload must not allow rebinding the template's venue."""
        # Arrange
        series = _make_series_with_rule(organization)
        assert series.template_event is not None
        original_venue_id = series.template_event.venue_id

        url = reverse(
            "api:update_series_template",
            kwargs={"slug": organization.slug, "series_id": str(series.id)},
        )
        payload = {"venue_id": str(uuid4())}

        # Act
        response = organization_owner_client.patch(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert — deterministic 422 from ``extra="forbid"``.
        assert response.status_code == 422
        series.refresh_from_db()
        assert series.template_event is not None
        assert series.template_event.venue_id == original_venue_id

    def test_update_template_rejects_status_change(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Templates must stay in DRAFT — the edit schema deliberately excludes ``status``."""
        # Arrange
        series = _make_series_with_rule(organization)
        assert series.template_event is not None
        original_status = series.template_event.status

        url = reverse(
            "api:update_series_template",
            kwargs={"slug": organization.slug, "series_id": str(series.id)},
        )
        payload = {"status": "open"}

        # Act
        response = organization_owner_client.patch(
            url,
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        # Assert — deterministic 422 from ``extra="forbid"``; status unchanged.
        assert response.status_code == 422
        series.refresh_from_db()
        assert series.template_event is not None
        assert series.template_event.status == original_status
