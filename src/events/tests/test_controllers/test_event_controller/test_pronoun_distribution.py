"""Tests for GET /events/{event_id}/pronoun-distribution endpoint access control."""

import typing as t

import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import Event

pytestmark = pytest.mark.django_db


def _url(event: Event) -> str:
    return reverse("api:event_pronoun_distribution", args=[event.id])


_REDACTED: dict[str, list[t.Any] | None] = {
    "distribution": [],
    "total_with_pronouns": None,
    "total_without_pronouns": None,
    "total_attendees": None,
}


class TestPronounDistributionAccessControl:
    """Gating via ``visibility_settings.show_pronoun_distribution`` (#793)."""

    def test_owner_sees_real_numbers_when_disabled(self, organization_owner_client: Client, event: Event) -> None:
        """Org owner keeps the operational view even when the event opts out."""
        assert event.visibility_flags.show_pronoun_distribution is False

        response = organization_owner_client.get(_url(event))

        assert response.status_code == 200
        assert response.json()["total_attendees"] is not None

    def test_staff_sees_real_numbers_when_disabled(self, organization_staff_client: Client, event: Event) -> None:
        """Org staff keep the operational view even when the event opts out."""
        response = organization_staff_client.get(_url(event))

        assert response.status_code == 200
        assert response.json()["total_attendees"] is not None

    def test_nonmember_gets_redacted_body_not_403(self, nonmember_client: Client, event: Event) -> None:
        """Converged onto the #792 redaction pattern: 200 with nothing in it."""
        response = nonmember_client.get(_url(event))

        assert response.status_code == 200
        assert response.json() == _REDACTED

    def test_member_gets_redacted_body_not_403(self, member_client: Client, event: Event) -> None:
        """Org membership alone does not unlock an opted-out distribution."""
        response = member_client.get(_url(event))

        assert response.status_code == 200
        assert response.json() == _REDACTED

    def test_nonmember_can_access_when_enabled(self, nonmember_client: Client, event: Event) -> None:
        """Any authenticated user gets the distribution once the event opts in."""
        event.visibility_settings = {"show_pronoun_distribution": True}
        event.save(update_fields=["visibility_settings"])

        response = nonmember_client.get(_url(event))

        assert response.status_code == 200
        assert response.json()["total_attendees"] is not None

    def test_member_can_access_when_enabled(self, member_client: Client, event: Event) -> None:
        """Org member gets the distribution once the event opts in."""
        event.visibility_settings = {"show_pronoun_distribution": True}
        event.save(update_fields=["visibility_settings"])

        response = member_client.get(_url(event))

        assert response.status_code == 200
        assert response.json()["total_attendees"] is not None

    def test_owner_can_access_when_enabled(self, organization_owner_client: Client, event: Event) -> None:
        """Org owner still sees it when the event opts in."""
        event.visibility_settings = {"show_pronoun_distribution": True}
        event.save(update_fields=["visibility_settings"])

        response = organization_owner_client.get(_url(event))

        assert response.status_code == 200

    def test_enabled_but_counts_hidden_is_still_redacted(self, nonmember_client: Client, event: Event) -> None:
        """The two gates AND: opting in must not sidestep an event that hides counts."""
        event.visibility_settings = {"show_pronoun_distribution": True, "show_attendee_count": False}
        event.save(update_fields=["visibility_settings"])

        response = nonmember_client.get(_url(event))

        assert response.status_code == 200
        assert response.json() == _REDACTED

    def test_unauthenticated_gets_401(self, event: Event) -> None:
        """Unauthenticated user gets 401 regardless of the toggle."""
        response = Client().get(_url(event))

        assert response.status_code == 401
