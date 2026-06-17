"""Tests for the event admin schedule endpoint."""

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import Event

pytestmark = pytest.mark.django_db


def _url(event: Event) -> str:
    return reverse("api:update_event_schedule", kwargs={"event_id": event.pk})


def test_owner_can_replace_schedule(organization_owner_client: Client, event: Event) -> None:
    payload = {
        "sessions": [
            {"title": "Arrival", "offset_minutes": 0},
            {"title": "Workshop", "offset_minutes": 60, "duration_minutes": 90, "is_required": True},
        ]
    }
    response = organization_owner_client.put(_url(event), data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 200, response.content
    body = response.json()
    assert len(body["schedule"]) == 2
    assert body["schedule"][1]["is_required"] is True
    event.refresh_from_db()
    assert len(event.schedule) == 2


def test_empty_sessions_clears_schedule(organization_owner_client: Client, event: Event) -> None:
    event.schedule = [{"title": "Old", "offset_minutes": 0}]
    event.save(update_fields=["schedule"])
    response = organization_owner_client.put(
        _url(event), data=orjson.dumps({"sessions": []}), content_type="application/json"
    )
    assert response.status_code == 200, response.content
    event.refresh_from_db()
    assert event.schedule == []


def test_malformed_session_rejected(organization_owner_client: Client, event: Event) -> None:
    payload = {"sessions": [{"offset_minutes": 0}]}  # missing title
    response = organization_owner_client.put(_url(event), data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 422, response.content


def test_description_is_bleached(organization_owner_client: Client, event: Event) -> None:
    payload = {"sessions": [{"title": "X", "offset_minutes": 0, "description": "<script>alert(1)</script>hi"}]}
    response = organization_owner_client.put(_url(event), data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 200, response.content
    event.refresh_from_db()
    assert "<script>" not in event.schedule[0]["description"]


def test_non_member_forbidden(nonmember_client: Client, event: Event) -> None:
    payload: dict[str, list[object]] = {"sessions": []}
    response = nonmember_client.put(_url(event), data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code in (401, 403, 404), response.content


def test_too_many_sessions_rejected(organization_owner_client: Client, event: Event) -> None:
    payload = {"sessions": [{"title": f"S{i}", "offset_minutes": i} for i in range(201)]}
    response = organization_owner_client.put(_url(event), data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 422, response.content
