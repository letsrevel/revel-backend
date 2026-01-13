"""Tests for event invitation request management endpoints."""

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from events.models import Event, EventInvitationRequest

pytestmark = pytest.mark.django_db


# --- Tests for POST /event-admin/{event_id}/invitation-request/{request_id}/{decision} ---


def test_decide_invitation_request_approve(
    organization_owner_client: Client, event_invitation_request: EventInvitationRequest
) -> None:
    """Test approving an invitation request."""
    url = reverse(
        "api:approve_invitation_request",
        kwargs={
            "event_id": event_invitation_request.event.pk,
            "request_id": event_invitation_request.pk,
        },
    )
    response = organization_owner_client.post(url)
    assert response.status_code == 204
    event_invitation_request.refresh_from_db()
    assert event_invitation_request.status == EventInvitationRequest.InvitationRequestStatus.APPROVED


def test_decide_invitation_request_reject(
    organization_owner_client: Client, event_invitation_request: EventInvitationRequest
) -> None:
    """Test rejecting an invitation request."""
    url = reverse(
        "api:reject_invitation_request",
        kwargs={
            "event_id": event_invitation_request.event.pk,
            "request_id": event_invitation_request.pk,
        },
    )
    response = organization_owner_client.post(url)
    assert response.status_code == 204
    event_invitation_request.refresh_from_db()
    assert event_invitation_request.status == EventInvitationRequest.InvitationRequestStatus.REJECTED


# --- Tests for GET /event-admin/{event_id}/invitation-requests ---


def test_list_event_invitation_requests(
    organization_owner_client: Client, public_event: Event, event_invitation_request: EventInvitationRequest
) -> None:
    """Test listing event invitation requests."""
    url = reverse("api:list_invitation_requests", kwargs={"event_id": public_event.pk})
    response = organization_owner_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(event_invitation_request.pk)
