"""Tests for the read-only recurring events endpoints.

Covers:
- ``GET /organization-admin/{slug}/event-series/{series_id}`` — admin detail.
- ``GET /organization-admin/{slug}/event-series/{series_id}/drift`` — drift.

Kept in a separate module from the mutation tests (create, lifecycle, template)
so files stay well under the project's 1000-line limit.
"""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, EventSeries, Organization, RecurrenceRule

from ._recurring_events_helpers import _make_series_with_rule

pytestmark = pytest.mark.django_db


class TestGetSeriesDetail:
    """Tests for ``GET /organization-admin/{slug}/event-series/{series_id}``.

    The endpoint is the read counterpart to the mutation endpoints that already
    return ``EventSeriesRecurrenceDetailSchema``. These tests verify it returns
    the expected shape, enforces the ``edit_event_series`` permission, and 404s
    cleanly when the series doesn't exist.
    """

    def test_owner_gets_full_shape(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """The owner receives the full admin-detail payload, not the public shape."""
        # Arrange
        series = _make_series_with_rule(organization)
        url = reverse(
            "api:get_series_detail",
            kwargs={"slug": organization.slug, "series_id": str(series.id)},
        )

        # Act
        response = organization_owner_client.get(url)

        # Assert — fields that exist only on the admin schema must all be present
        # so a regression silently dropping one of them is caught here.
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(series.id)
        assert data["name"] == series.name
        assert data["generation_window_weeks"] == 4
        assert data["is_active"] is True
        assert data["auto_publish"] is False
        assert data["exdates"] == []
        assert data["recurrence_rule"] is not None
        assert data["recurrence_rule"]["frequency"] == "daily"
        assert data["template_event"] is not None
        assert data["template_event"]["id"] == str(series.template_event_id)

    def test_returns_404_for_nonmember(
        self,
        nonmember_client: Client,
        organization: Organization,
    ) -> None:
        """A non-member gets 404 (not 403) — same invisibility contract as the mutations."""
        # Arrange — build a real series so the assertion covers the authorisation
        # branch rather than "missing resource returns 404".
        series = _make_series_with_rule(organization)
        url = reverse(
            "api:get_series_detail",
            kwargs={"slug": organization.slug, "series_id": str(series.id)},
        )

        # Act
        response = nonmember_client.get(url)

        # Assert
        assert response.status_code == 404

    def test_returns_404_for_missing_series(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """A random uuid returns 404 rather than leaking any org state."""
        url = reverse(
            "api:get_series_detail",
            kwargs={"slug": organization.slug, "series_id": str(uuid4())},
        )
        response = organization_owner_client.get(url)
        assert response.status_code == 404

    def test_returns_404_for_wrong_org_series(
        self,
        organization_owner_client: Client,
        organization: Organization,
        organization_owner_user: RevelUser,
    ) -> None:
        """A series that belongs to a different organization must not be reachable.

        The controller filters by ``organization=...`` in ``_get_series``; this
        exercises that filter directly so a regression that drops it (returning
        the series when the slug/uuid pair belongs to two different orgs) trips
        this test rather than leaking cross-org data.
        """
        # Arrange — build a second org with its own series and point the request
        # at the first org's slug + the second org's series id.
        other_org = Organization.objects.create(
            name="Other",
            slug="other",
            owner=organization_owner_user,
        )
        other_series = _make_series_with_rule(other_org)
        url = reverse(
            "api:get_series_detail",
            kwargs={"slug": organization.slug, "series_id": str(other_series.id)},
        )

        # Act
        response = organization_owner_client.get(url)

        # Assert
        assert response.status_code == 404


class TestGetSeriesDrift:
    """Tests for ``GET /organization-admin/{slug}/event-series/{series_id}/drift``."""

    @staticmethod
    def _make_occurrence(
        *,
        organization: Organization,
        series: EventSeries,
        start: datetime,
        status: str = Event.EventStatus.DRAFT,
        is_modified: bool = False,
    ) -> Event:
        """Build a materialized (non-template) occurrence at an exact instant.

        Controller-level tests can't freeze time (that would invalidate the
        JWT), so occurrences must be anchored to wall-clock-relative
        datetimes. Creating the events directly via the ORM (rather than via
        ``generate_series_events``) lets us position each occurrence exactly
        on/off the rule.
        """
        return Event.objects.create(
            organization=organization,
            event_series=series,
            name="Occurrence",
            start=start,
            end=start + timedelta(hours=2),
            status=status,
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            is_template=False,
            is_modified=is_modified,
        )

    def test_returns_empty_when_events_match_rule(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """If all future events sit on the rule, the response is an empty list.

        ``_make_series_with_rule`` uses a DAILY rule; placing events at
        ``dtstart + Nd`` means they line up exactly with the rule's instants.
        """
        # Arrange — dtstart in the future so events count as "future" under now().
        dtstart = timezone.now().replace(microsecond=0) + timedelta(days=1)
        series = _make_series_with_rule(organization, dtstart=dtstart)
        assert series.recurrence_rule is not None
        self._make_occurrence(organization=organization, series=series, start=dtstart + timedelta(days=1))
        self._make_occurrence(organization=organization, series=series, start=dtstart + timedelta(days=2))

        url = reverse(
            "api:get_series_drift",
            kwargs={"slug": organization.slug, "series_id": str(series.id)},
        )

        # Act
        response = organization_owner_client.get(url)

        # Assert
        assert response.status_code == 200
        assert response.json() == {"stale_occurrences": []}

    def test_returns_stale_occurrence_ids(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Events sitting off the rule appear in ``stale_occurrences``.

        The DAILY rule at ``dtstart 10:00`` produces instants at 10:00 on each
        day. An event placed at ``dtstart + 1d + 12h`` (i.e. 22:00, half a day
        off the cadence) is not produced by the rule and must drift.
        """
        # Arrange
        dtstart = (timezone.now() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        series = _make_series_with_rule(organization, dtstart=dtstart)
        # On-cadence event — must NOT drift.
        on_cadence = self._make_occurrence(organization=organization, series=series, start=dtstart + timedelta(days=1))
        # Off-cadence event — same day, 12h later. MUST drift.
        off_cadence = self._make_occurrence(
            organization=organization, series=series, start=dtstart + timedelta(days=1, hours=12)
        )

        url = reverse(
            "api:get_series_drift",
            kwargs={"slug": organization.slug, "series_id": str(series.id)},
        )

        # Act
        response = organization_owner_client.get(url)

        # Assert
        assert response.status_code == 200
        stale = response.json()["stale_occurrences"]
        assert str(off_cadence.id) in stale
        assert str(on_cadence.id) not in stale

    def test_returns_404_for_nonmember(
        self,
        nonmember_client: Client,
        organization: Organization,
    ) -> None:
        """A non-member gets 404 — authorisation runs before drift computation."""
        series = _make_series_with_rule(organization)
        url = reverse(
            "api:get_series_drift",
            kwargs={"slug": organization.slug, "series_id": str(series.id)},
        )
        response = nonmember_client.get(url)
        assert response.status_code == 404

    def test_empty_when_no_recurrence_rule(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """A series with no rule cannot drift — the endpoint returns an empty list
        without raising. This guards the cheap pre-check in ``detect_cadence_drift``
        that avoids ``rule.between`` on a null rule.
        """
        # Arrange — build the series by hand so we can leave ``recurrence_rule=None``.
        series = _make_series_with_rule(organization)
        rule: RecurrenceRule = series.recurrence_rule  # type: ignore[assignment]
        series.recurrence_rule = None
        series.save(update_fields=["recurrence_rule"])
        rule.delete()

        url = reverse(
            "api:get_series_drift",
            kwargs={"slug": organization.slug, "series_id": str(series.id)},
        )
        response = organization_owner_client.get(url)
        assert response.status_code == 200
        assert response.json() == {"stale_occurrences": []}
