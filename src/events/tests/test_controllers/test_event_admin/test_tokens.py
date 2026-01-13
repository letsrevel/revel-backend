"""Tests for event token management endpoints."""

import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from events.models import Event, EventToken, TicketTier

pytestmark = pytest.mark.django_db


# --- Tests for POST /event-admin/{event_id}/token ---


def test_create_event_token(organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier) -> None:
    """Test creating an event token."""
    url = reverse("api:create_event_token", kwargs={"event_id": event.pk})
    payload = {"name": "Test Token", "max_uses": 10, "ticket_tier_id": str(event_ticket_tier.id)}
    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Test Token"
    assert EventToken.objects.filter(pk=data["id"]).exists()


def test_create_event_token_with_full_payload_including_invitation(
    organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier
) -> None:
    """Regression test: ensure full payload with invitation_payload doesn't cause 500.

    This test ensures that when the API endpoint receives a full payload including
    invitation_payload (which is already converted to a dict by pydantic's model_dump),
    the service layer correctly handles it without trying to call model_dump again.
    """
    url = reverse("api:create_event_token", kwargs={"event_id": event.pk})
    payload = {
        "name": "Token with Invitation",
        "max_uses": 5,
        "grants_invitation": True,
        "invitation_payload": {
            "waives_questionnaire": True,
            "overrides_max_attendees": False,
        },
        "ticket_tier_id": str(event_ticket_tier.id),
    }
    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    data = response.json()
    assert data["name"] == "Token with Invitation"
    assert data["grants_invitation"] is True
    assert data["invitation_payload"] is not None
    assert data["invitation_payload"]["waives_questionnaire"] is True
    assert data["invitation_payload"]["overrides_max_attendees"] is False

    # Verify the token was created in the database with correct invitation_payload
    token = EventToken.objects.get(pk=data["id"])
    assert token.invitation_payload is not None
    assert token.invitation_payload["waives_questionnaire"] is True
    assert token.invitation_payload["overrides_max_attendees"] is False


# --- Tests for GET /event-admin/{event_id}/tokens ---


def test_list_event_tokens(organization_owner_client: Client, event: Event, event_token: EventToken) -> None:
    """Test listing event tokens."""
    url = reverse("api:list_event_tokens", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(event_token.pk)


# --- Tests for PUT /event-admin/token/{token_id} ---


def test_update_event_token(organization_owner_client: Client, event_token: EventToken) -> None:
    """Test updating an event token."""
    url = reverse("api:edit_event_token", kwargs={"event_id": event_token.event_id, "token_id": event_token.pk})
    payload = {"name": "Updated Token Name"}
    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 200, response.json()
    event_token.refresh_from_db()
    assert event_token.name == "Updated Token Name"


# --- Tests for DELETE /event-admin/token/{token_id} ---


def test_delete_event_token(organization_owner_client: Client, event_token: EventToken) -> None:
    """Test deleting an event token."""
    url = reverse("api:delete_event_token", kwargs={"event_id": event_token.event_id, "token_id": event_token.pk})
    response = organization_owner_client.delete(url)
    assert response.status_code == 204, response.text
    assert not EventToken.objects.filter(pk=event_token.pk).exists()
