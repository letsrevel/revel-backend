"""Tests for the RSVP note on POST /events/{event_id}/rsvp/{answer} and /my-status."""

import typing as t

import pytest
from django.test.client import Client
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Event, EventRSVP

pytestmark = pytest.mark.django_db


@pytest.fixture
def user_client(revel_user_factory: t.Any) -> tuple[Client, RevelUser]:
    """Authenticated client for a user with no organization relationships."""
    user = revel_user_factory()
    refresh = RefreshToken.for_user(user)
    client = Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]
    return client, user


@pytest.fixture
def notes_event(public_event: Event) -> Event:
    """Public non-ticketed event with RSVP notes enabled."""
    public_event.requires_ticket = False
    public_event.accept_rsvp_notes = True
    public_event.save()
    return public_event


def _rsvp_url(event: Event, answer: str = "yes") -> str:
    return reverse("api:rsvp_event", kwargs={"event_id": str(event.id), "answer": answer})


class TestRSVPNoteEndpoint:
    def test_rsvp_with_note_stores_and_returns_it(
        self, user_client: tuple[Client, RevelUser], notes_event: Event
    ) -> None:
        client, user = user_client

        response = client.post(_rsvp_url(notes_event), data={"note": "bringing a +1"}, content_type="application/json")

        assert response.status_code == 200
        assert response.json()["note"] == "bringing a +1"
        assert EventRSVP.objects.get(event=notes_event, user=user).note == "bringing a +1"

    def test_rsvp_without_body_still_works(self, user_client: tuple[Client, RevelUser], notes_event: Event) -> None:
        client, _user = user_client

        response = client.post(_rsvp_url(notes_event), content_type="application/json")

        assert response.status_code == 200
        assert response.json()["note"] == ""

    def test_note_rejected_when_flag_off(self, user_client: tuple[Client, RevelUser], public_event: Event) -> None:
        public_event.requires_ticket = False
        public_event.save()
        client, user = user_client

        response = client.post(_rsvp_url(public_event), data={"note": "hi"}, content_type="application/json")

        assert response.status_code == 400
        assert not EventRSVP.objects.filter(event=public_event, user=user).exists()

    def test_repost_without_note_clears_it(self, user_client: tuple[Client, RevelUser], notes_event: Event) -> None:
        client, user = user_client
        client.post(_rsvp_url(notes_event), data={"note": "old note"}, content_type="application/json")

        response = client.post(_rsvp_url(notes_event, "maybe"), content_type="application/json")

        assert response.status_code == 200
        assert EventRSVP.objects.get(event=notes_event, user=user).note == ""

    def test_note_over_500_chars_is_rejected(self, user_client: tuple[Client, RevelUser], notes_event: Event) -> None:
        client, _user = user_client

        response = client.post(_rsvp_url(notes_event), data={"note": "x" * 501}, content_type="application/json")

        assert response.status_code == 422

    def test_note_surfaces_in_my_status(self, user_client: tuple[Client, RevelUser], notes_event: Event) -> None:
        client, _user = user_client
        client.post(_rsvp_url(notes_event), data={"note": "gluten free"}, content_type="application/json")

        url = reverse("api:get_my_event_status", kwargs={"event_id": str(notes_event.id)})
        response = client.get(url)

        assert response.status_code == 200
        assert response.json()["rsvp"]["note"] == "gluten free"
