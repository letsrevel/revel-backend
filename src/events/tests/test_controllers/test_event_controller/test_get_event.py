"""Tests for GET /events/{slug}/ endpoint."""

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from events.models import (
    Event,
    EventInvitation,
)

pytestmark = pytest.mark.django_db


def test_get_event_visibility(
    client: Client,
    nonmember_client: Client,
    member_client: Client,
    nonmember_user: RevelUser,
    public_event: Event,
    private_event: Event,
    members_only_event: Event,
) -> None:
    """Test retrieving a single event based on visibility rules."""
    # Invite the nonmember_user to the private event
    EventInvitation.objects.create(user=nonmember_user, event=private_event)

    # --- Assertions for Public Event ---
    public_url = reverse("api:get_event", kwargs={"event_id": public_event.pk})
    assert client.get(public_url).status_code == 200
    assert nonmember_client.get(public_url).status_code == 200
    assert member_client.get(public_url).status_code == 200

    # --- Assertions for Private Event ---
    private_url = reverse("api:get_event", kwargs={"event_id": private_event.pk})
    assert client.get(private_url).status_code == 404  # Anonymous can't see
    assert member_client.get(private_url).status_code == 404  # Member can't see without invite
    assert nonmember_client.get(private_url).status_code == 200  # Invited user can see

    # --- Assertions for Members-Only Event ---
    members_url = reverse("api:get_event", kwargs={"event_id": members_only_event.pk})
    assert client.get(members_url).status_code == 404  # Anonymous can't see
    assert nonmember_client.get(members_url).status_code == 404  # Non-member can't see
    assert member_client.get(members_url).status_code == 200  # Member can see
