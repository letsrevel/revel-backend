"""Tests for GET /events/{event_id}/pronoun-distribution endpoint access control."""

import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import Event

pytestmark = pytest.mark.django_db


def _url(event: Event) -> str:
    return reverse("api:event_pronoun_distribution", args=[event.id])


class TestPronounDistributionAccessControl:
    """Tests for the public_pronoun_distribution flag gating."""

    def test_owner_can_access_when_disabled(
        self,
        organization_owner_client: Client,
        event: Event,
    ) -> None:
        """Org owner can access pronoun distribution even when flag is False."""
        assert event.public_pronoun_distribution is False
        response = organization_owner_client.get(_url(event))
        assert response.status_code == 200

    def test_staff_can_access_when_disabled(
        self,
        organization_staff_client: Client,
        event: Event,
    ) -> None:
        """Org staff can access pronoun distribution even when flag is False."""
        assert event.public_pronoun_distribution is False
        response = organization_staff_client.get(_url(event))
        assert response.status_code == 200

    def test_nonmember_gets_403_when_disabled(
        self,
        nonmember_client: Client,
        event: Event,
    ) -> None:
        """Non-staff authenticated user gets 403 when flag is False."""
        assert event.public_pronoun_distribution is False
        response = nonmember_client.get(_url(event))
        assert response.status_code == 403

    def test_member_gets_403_when_disabled(
        self,
        member_client: Client,
        event: Event,
    ) -> None:
        """Org member (non-staff) gets 403 when flag is False."""
        assert event.public_pronoun_distribution is False
        response = member_client.get(_url(event))
        assert response.status_code == 403

    def test_nonmember_can_access_when_enabled(
        self,
        nonmember_client: Client,
        event: Event,
    ) -> None:
        """Any authenticated user can access when flag is True."""
        event.public_pronoun_distribution = True
        event.save(update_fields=["public_pronoun_distribution"])
        response = nonmember_client.get(_url(event))
        assert response.status_code == 200

    def test_member_can_access_when_enabled(
        self,
        member_client: Client,
        event: Event,
    ) -> None:
        """Org member can access when flag is True."""
        event.public_pronoun_distribution = True
        event.save(update_fields=["public_pronoun_distribution"])
        response = member_client.get(_url(event))
        assert response.status_code == 200

    def test_owner_can_access_when_enabled(
        self,
        organization_owner_client: Client,
        event: Event,
    ) -> None:
        """Org owner can still access when flag is True."""
        event.public_pronoun_distribution = True
        event.save(update_fields=["public_pronoun_distribution"])
        response = organization_owner_client.get(_url(event))
        assert response.status_code == 200

    def test_unauthenticated_gets_401(
        self,
        event: Event,
    ) -> None:
        """Unauthenticated user gets 401 regardless of flag."""
        client = Client()
        response = client.get(_url(event))
        assert response.status_code == 401
