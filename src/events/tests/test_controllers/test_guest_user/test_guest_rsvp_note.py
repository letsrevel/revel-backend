"""Tests for the RSVP note in the guest RSVP flow."""

import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Event, EventRSVP
from events.service import guest as guest_service

pytestmark = pytest.mark.django_db


@pytest.fixture
def notes_guest_event(guest_event: Event) -> Event:
    """Guest-accessible event with RSVP notes enabled."""
    guest_event.accept_rsvp_notes = True
    guest_event.save()
    return guest_event


def _guest_rsvp_url(event: Event, answer: str = "yes") -> str:
    return reverse("api:guest_rsvp", kwargs={"event_id": str(event.id), "answer": answer})


def test_guest_rsvp_with_note_flag_off_is_rejected(guest_event: Event) -> None:
    """Submitting a note while the event does not accept notes returns 400."""
    client = Client()
    payload = {"email": "guest@example.com", "first_name": "Gia", "last_name": "Guest", "note": "hi"}

    response = client.post(_guest_rsvp_url(guest_event), data=payload, content_type="application/json")

    assert response.status_code == 400


def test_guest_rsvp_note_survives_jwt_roundtrip(notes_guest_event: Event, existing_guest_user: RevelUser) -> None:
    """The note stored in the confirmation token lands on the RSVP at confirmation."""
    token = guest_service.create_guest_rsvp_token(
        existing_guest_user, notes_guest_event.id, "yes", note="no onions please"
    )

    client = Client()
    response = client.post(reverse("api:confirm_guest_action"), data={"token": token}, content_type="application/json")

    assert response.status_code == 200
    assert response.json()["note"] == "no onions please"
    rsvp = EventRSVP.objects.get(user=existing_guest_user, event=notes_guest_event)
    assert rsvp.note == "no onions please"


def test_guest_note_dropped_if_flag_disabled_before_confirmation(
    notes_guest_event: Event, existing_guest_user: RevelUser
) -> None:
    """Organizer disabling notes between email-send and click drops the note, keeps the RSVP."""
    token = guest_service.create_guest_rsvp_token(existing_guest_user, notes_guest_event.id, "yes", note="late note")
    notes_guest_event.accept_rsvp_notes = False
    notes_guest_event.save()

    client = Client()
    response = client.post(reverse("api:confirm_guest_action"), data={"token": token}, content_type="application/json")

    assert response.status_code == 200
    rsvp = EventRSVP.objects.get(user=existing_guest_user, event=notes_guest_event)
    assert rsvp.status == EventRSVP.RsvpStatus.YES
    assert rsvp.note == ""


def test_legacy_token_without_note_still_validates(guest_event: Event, existing_guest_user: RevelUser) -> None:
    """Tokens minted before the note field existed keep working (default "")."""
    token = guest_service.create_guest_rsvp_token(existing_guest_user, guest_event.id, "yes")

    client = Client()
    response = client.post(reverse("api:confirm_guest_action"), data={"token": token}, content_type="application/json")

    assert response.status_code == 200
    assert EventRSVP.objects.get(user=existing_guest_user, event=guest_event).note == ""
