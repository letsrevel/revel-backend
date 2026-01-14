"""Tests for invitation request endpoints."""

from datetime import timedelta

import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from django.utils import timezone

from accounts.models import RevelUser
from events import models
from events.models import Event

pytestmark = pytest.mark.django_db


# --- Tests for POST /events/{event_id}/invitation-requests ---


def test_request_invitation_success(nonmember_client: Client, public_event: Event) -> None:
    """Test that a user can successfully request an invitation to a private event."""
    url = reverse("api:create_invitation_request", kwargs={"event_id": public_event.pk})
    payload = {"message": "Please let me in!"}
    response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 201
    data = response.json()
    assert data["message"] == "Please let me in!"
    assert models.EventInvitationRequest.objects.filter(event=public_event, user__username="nonmember_user").exists()


def test_request_invitation_duplicate_fails(
    nonmember_client: Client, nonmember_user: RevelUser, public_event: Event
) -> None:
    """Test that requesting an invitation twice for the same event fails."""
    # First request
    models.EventInvitationRequest.objects.create(event=public_event, user=nonmember_user, message="First try")

    url = reverse("api:create_invitation_request", kwargs={"event_id": public_event.pk})
    payload = {"message": "Second try"}
    response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 400
    assert "You have already requested an invitation to this event" in response.json()["detail"]
    assert models.EventInvitationRequest.objects.count() == 1


def test_request_invitation_fails_after_deadline(nonmember_client: Client, public_event: Event) -> None:
    """Test that requesting an invitation fails when application deadline has passed."""
    public_event.apply_before = timezone.now() - timedelta(hours=1)
    public_event.save()

    url = reverse("api:create_invitation_request", kwargs={"event_id": public_event.pk})
    payload = {"message": "Please let me in!"}
    response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 400
    assert "deadline has passed" in response.json()["detail"]
    assert models.EventInvitationRequest.objects.count() == 0


def test_request_invitation_succeeds_before_deadline(nonmember_client: Client, public_event: Event) -> None:
    """Test that requesting an invitation succeeds when deadline has not passed."""
    public_event.apply_before = timezone.now() + timedelta(hours=1)
    public_event.save()

    url = reverse("api:create_invitation_request", kwargs={"event_id": public_event.pk})
    payload = {"message": "Please let me in!"}
    response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 201
    assert models.EventInvitationRequest.objects.count() == 1


# --- Tests for GET /events/invitation-requests ---


def test_get_my_pending_invitation_requests_success(
    nonmember_client: Client, nonmember_user: RevelUser, private_event: Event, public_event: Event
) -> None:
    """Test that a user can retrieve their own pending invitation requests."""
    # Create two requests for the user
    request1 = models.EventInvitationRequest.objects.create(event=private_event, user=nonmember_user, message="Req 1")
    models.EventInvitationRequest.objects.create(event=public_event, user=nonmember_user, message="Req 2")

    # Create a request for another user to ensure it's not included
    other_user = RevelUser.objects.create_user("otheruser")
    models.EventInvitationRequest.objects.create(event=private_event, user=other_user, message="Other user req")

    url = reverse("api:dashboard_invitation_requests")
    response = nonmember_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2
    results = data["results"]
    assert {r["id"] for r in results} == {
        str(request1.id),
        str(models.EventInvitationRequest.objects.get(event=public_event).id),
    }


def test_get_my_pending_invitation_requests_search_and_filter(
    nonmember_client: Client, nonmember_user: RevelUser, private_event: Event, public_event: Event
) -> None:
    """Test filtering and searching the user's pending invitation requests."""
    models.EventInvitationRequest.objects.create(event=private_event, user=nonmember_user, message="Looking for tech")
    models.EventInvitationRequest.objects.create(event=public_event, user=nonmember_user, message="Looking for art")

    url = reverse("api:dashboard_invitation_requests")

    # Filter by event_id
    response = nonmember_client.get(url, {"event_id": str(private_event.pk)})
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["results"][0]["event"]["id"] == str(private_event.pk)

    # Search by message content
    response = nonmember_client.get(url, {"search": "tech"})
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["results"][0]["message"] == "Looking for tech"

    # Search by event name
    response = nonmember_client.get(url, {"search": public_event.name})
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["results"][0]["event"]["name"] == public_event.name


def test_get_my_pending_invitation_requests_anonymous_fails(client: Client) -> None:
    """Test that an anonymous user cannot retrieve pending requests."""
    url = reverse("api:dashboard_invitation_requests")
    response = client.get(url)
    assert response.status_code == 401


def test_get_my_invitation_requests_status_filtering(
    nonmember_client: Client,
    nonmember_user: RevelUser,
    private_event: Event,
    public_event: Event,
    members_only_event: Event,
) -> None:
    """Test that status filtering defaults to pending but can show all statuses."""
    # Create requests with different statuses
    pending_req = models.EventInvitationRequest.objects.create(
        event=private_event,
        user=nonmember_user,
        message="Pending",
        status=models.EventInvitationRequest.InvitationRequestStatus.PENDING,
    )
    approved_req = models.EventInvitationRequest.objects.create(
        event=public_event,
        user=nonmember_user,
        message="Approved",
        status=models.EventInvitationRequest.InvitationRequestStatus.APPROVED,
    )
    rejected_req = models.EventInvitationRequest.objects.create(
        event=members_only_event,
        user=nonmember_user,
        message="Rejected",
        status=models.EventInvitationRequest.InvitationRequestStatus.REJECTED,
    )

    url = reverse("api:dashboard_invitation_requests")

    response = nonmember_client.get(url, {"status": "pending"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(pending_req.id)
    assert data["results"][0]["status"] == "pending"

    # Filter by approved
    response = nonmember_client.get(url, {"status": "approved"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(approved_req.id)
    assert data["results"][0]["status"] == "approved"

    # Filter by rejected
    response = nonmember_client.get(url, {"status": "rejected"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(rejected_req.id)
    assert data["results"][0]["status"] == "rejected"
