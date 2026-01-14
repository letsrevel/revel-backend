"""Tests for POST /events/{event_id}/claim-invitation/{token} endpoint."""

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from events.models import (
    EventInvitation,
    EventToken,
)

pytestmark = pytest.mark.django_db


class TestClaimInvitation:
    def test_claim_invitation_success(
        self, nonmember_client: Client, event_token: EventToken, nonmember_user: RevelUser
    ) -> None:
        """Test that an invitation is claimed successfully."""
        event_token.invitation_payload = {}
        event_token.save()
        url = reverse("api:event_claim_invitation", kwargs={"token": event_token.id})
        response = nonmember_client.post(url)
        assert response.status_code == 200
        assert EventInvitation.objects.filter(event=event_token.event, user=nonmember_user).exists()

    def test_claim_invitation_unauthorized(self, client: Client, event_token: EventToken) -> None:
        """Test that an unauthenticated user cannot claim an invitation."""
        url = reverse("api:event_claim_invitation", kwargs={"token": event_token.id})
        response = client.post(url)
        assert response.status_code == 401

    def test_claim_invitation_invalid_token(self, nonmember_client: Client) -> None:
        """Test that an invalid token returns a 400."""
        url = reverse("api:event_claim_invitation", kwargs={"token": "invalid-token"})
        response = nonmember_client.post(url)
        assert response.status_code == 400
