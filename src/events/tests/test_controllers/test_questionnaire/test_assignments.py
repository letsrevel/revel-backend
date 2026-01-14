"""Tests for questionnaire event and event series assignment operations."""

from datetime import timedelta

import orjson
import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, EventSeries, Organization, OrganizationQuestionnaire
from questionnaires.models import Questionnaire

pytestmark = pytest.mark.django_db


# --- Event assignment tests ---


def test_replace_events_success(
    organization: Organization, organization_owner_client: Client, event: Event, public_event: Event
) -> None:
    """Test that events can be batch replaced for a questionnaire."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    # Initially assign one event
    org_questionnaire.events.add(event)

    # Replace with two events
    payload = {"event_ids": [str(event.id), str(public_event.id)]}

    url = reverse("api:replace_questionnaire_events", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200

    # Verify events were replaced
    org_questionnaire.refresh_from_db()
    event_ids = list(org_questionnaire.events.values_list("id", flat=True))
    assert len(event_ids) == 2
    assert event.id in event_ids
    assert public_event.id in event_ids


def test_replace_events_invalid_event(organization: Organization, organization_owner_client: Client) -> None:
    """Test that replacing events with invalid event ID returns 400."""
    from uuid import uuid4

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = {"event_ids": [str(uuid4())]}

    url = reverse("api:replace_questionnaire_events", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 400


def test_replace_events_wrong_organization(
    organization: Organization, organization_owner_client: Client, organization_owner_user: RevelUser
) -> None:
    """Test that events from another organization cannot be assigned."""
    # Create another organization with an event
    other_org = Organization.objects.create(name="Other Org", slug="other-org", owner=organization_owner_user)
    other_event = Event.objects.create(
        organization=other_org,
        name="Other Event",
        slug="other-event",
        event_type=Event.EventType.PUBLIC,
        status="open",
        start=timezone.now(),
        end=timezone.now() + timedelta(hours=2),
    )

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = {"event_ids": [str(other_event.id)]}

    url = reverse("api:replace_questionnaire_events", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 400


def test_replace_events_permission_denied(organization: Organization, nonmember_client: Client) -> None:
    """Test that non-members cannot replace events."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload: dict[str, list[str]] = {"event_ids": []}

    url = reverse("api:replace_questionnaire_events", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = nonmember_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 404


def test_assign_event_success(organization: Organization, organization_owner_client: Client, event: Event) -> None:
    """Test that a single event can be assigned to a questionnaire."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse(
        "api:assign_questionnaire_event", kwargs={"org_questionnaire_id": org_questionnaire.id, "event_id": event.id}
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 200

    # Verify event was assigned
    org_questionnaire.refresh_from_db()
    assert event in org_questionnaire.events.all()


def test_assign_event_wrong_organization(
    organization: Organization, organization_owner_client: Client, organization_owner_user: RevelUser
) -> None:
    """Test that event from another organization cannot be assigned."""
    # Create another organization with an event
    other_org = Organization.objects.create(name="Other Org", slug="other-org", owner=organization_owner_user)
    other_event = Event.objects.create(
        organization=other_org,
        name="Other Event",
        slug="other-event",
        event_type=Event.EventType.PUBLIC,
        status="open",
        start=timezone.now(),
        end=timezone.now() + timedelta(hours=2),
    )

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse(
        "api:assign_questionnaire_event",
        kwargs={"org_questionnaire_id": org_questionnaire.id, "event_id": other_event.id},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 404


def test_assign_event_permission_denied(organization: Organization, nonmember_client: Client, event: Event) -> None:
    """Test that non-members cannot assign events."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse(
        "api:assign_questionnaire_event", kwargs={"org_questionnaire_id": org_questionnaire.id, "event_id": event.id}
    )
    response = nonmember_client.post(url)

    assert response.status_code == 404


def test_unassign_event_success(organization: Organization, organization_owner_client: Client, event: Event) -> None:
    """Test that a single event can be unassigned from a questionnaire."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    org_questionnaire.events.add(event)

    url = reverse(
        "api:unassign_questionnaire_event", kwargs={"org_questionnaire_id": org_questionnaire.id, "event_id": event.id}
    )
    response = organization_owner_client.delete(url)

    assert response.status_code == 204

    # Verify event was unassigned
    org_questionnaire.refresh_from_db()
    assert event not in org_questionnaire.events.all()


def test_unassign_event_permission_denied(organization: Organization, nonmember_client: Client, event: Event) -> None:
    """Test that non-members cannot unassign events."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    org_questionnaire.events.add(event)

    url = reverse(
        "api:unassign_questionnaire_event", kwargs={"org_questionnaire_id": org_questionnaire.id, "event_id": event.id}
    )
    response = nonmember_client.delete(url)

    assert response.status_code == 404


# --- Event series assignment tests ---


def test_replace_event_series_success(
    organization: Organization, organization_owner_client: Client, event_series: EventSeries
) -> None:
    """Test that event series can be batch replaced for a questionnaire."""
    # Create another event series
    other_series = EventSeries.objects.create(organization=organization, name="Other Series", slug="other-series")

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    # Initially assign one series
    org_questionnaire.event_series.add(event_series)

    # Replace with two series
    payload = {"event_series_ids": [str(event_series.id), str(other_series.id)]}

    url = reverse("api:replace_questionnaire_event_series", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200

    # Verify series were replaced
    org_questionnaire.refresh_from_db()
    series_ids = list(org_questionnaire.event_series.values_list("id", flat=True))
    assert len(series_ids) == 2
    assert event_series.id in series_ids
    assert other_series.id in series_ids


def test_replace_event_series_invalid_series(organization: Organization, organization_owner_client: Client) -> None:
    """Test that replacing event series with invalid ID returns 400."""
    from uuid import uuid4

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = {"event_series_ids": [str(uuid4())]}

    url = reverse("api:replace_questionnaire_event_series", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 400


def test_replace_event_series_wrong_organization(
    organization: Organization, organization_owner_client: Client, organization_owner_user: RevelUser
) -> None:
    """Test that event series from another organization cannot be assigned."""
    # Create another organization with a series
    other_org = Organization.objects.create(name="Other Org", slug="other-org", owner=organization_owner_user)
    other_series = EventSeries.objects.create(organization=other_org, name="Other Series", slug="other-series")

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = {"event_series_ids": [str(other_series.id)]}

    url = reverse("api:replace_questionnaire_event_series", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 400


def test_replace_event_series_permission_denied(organization: Organization, nonmember_client: Client) -> None:
    """Test that non-members cannot replace event series."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload: dict[str, list[str]] = {"event_series_ids": []}

    url = reverse("api:replace_questionnaire_event_series", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = nonmember_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 404


def test_assign_event_series_success(
    organization: Organization, organization_owner_client: Client, event_series: EventSeries
) -> None:
    """Test that a single event series can be assigned to a questionnaire."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse(
        "api:assign_questionnaire_event_series",
        kwargs={"org_questionnaire_id": org_questionnaire.id, "series_id": event_series.id},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 200

    # Verify series was assigned
    org_questionnaire.refresh_from_db()
    assert event_series in org_questionnaire.event_series.all()


def test_assign_event_series_wrong_organization(
    organization: Organization, organization_owner_client: Client, organization_owner_user: RevelUser
) -> None:
    """Test that event series from another organization cannot be assigned."""
    # Create another organization with a series
    other_org = Organization.objects.create(name="Other Org", slug="other-org", owner=organization_owner_user)
    other_series = EventSeries.objects.create(organization=other_org, name="Other Series", slug="other-series")

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse(
        "api:assign_questionnaire_event_series",
        kwargs={"org_questionnaire_id": org_questionnaire.id, "series_id": other_series.id},
    )
    response = organization_owner_client.post(url)

    assert response.status_code == 404


def test_assign_event_series_permission_denied(
    organization: Organization, nonmember_client: Client, event_series: EventSeries
) -> None:
    """Test that non-members cannot assign event series."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse(
        "api:assign_questionnaire_event_series",
        kwargs={"org_questionnaire_id": org_questionnaire.id, "series_id": event_series.id},
    )
    response = nonmember_client.post(url)

    assert response.status_code == 404


def test_unassign_event_series_success(
    organization: Organization, organization_owner_client: Client, event_series: EventSeries
) -> None:
    """Test that a single event series can be unassigned from a questionnaire."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    org_questionnaire.event_series.add(event_series)

    url = reverse(
        "api:unassign_questionnaire_event_series",
        kwargs={"org_questionnaire_id": org_questionnaire.id, "series_id": event_series.id},
    )
    response = organization_owner_client.delete(url)

    assert response.status_code == 204

    # Verify series was unassigned
    org_questionnaire.refresh_from_db()
    assert event_series not in org_questionnaire.event_series.all()


def test_unassign_event_series_permission_denied(
    organization: Organization, nonmember_client: Client, event_series: EventSeries
) -> None:
    """Test that non-members cannot unassign event series."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    org_questionnaire.event_series.add(event_series)

    url = reverse(
        "api:unassign_questionnaire_event_series",
        kwargs={"org_questionnaire_id": org_questionnaire.id, "series_id": event_series.id},
    )
    response = nonmember_client.delete(url)

    assert response.status_code == 404
