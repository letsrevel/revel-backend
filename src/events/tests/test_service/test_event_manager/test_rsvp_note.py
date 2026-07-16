"""Tests for RSVP note handling in EventManager.rsvp."""

import pytest
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, EventRSVP
from events.service.event_manager import EventManager

pytestmark = pytest.mark.django_db


@pytest.fixture
def rsvp_event(public_event: Event) -> Event:
    """Public non-ticketed event that accepts RSVP notes."""
    public_event.requires_ticket = False
    public_event.accept_rsvp_notes = True
    public_event.save()
    return public_event


def test_note_rejected_when_flag_off(public_user: RevelUser, public_event: Event) -> None:
    """A non-empty note on an event with notes disabled raises 400 and writes nothing."""
    public_event.requires_ticket = False
    public_event.accept_rsvp_notes = False
    public_event.save()
    manager = EventManager(user=public_user, event=public_event)

    with pytest.raises(HttpError) as exc_info:
        manager.rsvp(EventRSVP.RsvpStatus.YES, note="hello")

    assert exc_info.value.status_code == 400
    assert not EventRSVP.objects.filter(event=public_event, user=public_user).exists()


def test_empty_note_allowed_when_flag_off(public_user: RevelUser, public_event: Event) -> None:
    """An empty note never triggers the flag check."""
    public_event.requires_ticket = False
    public_event.save()
    manager = EventManager(user=public_user, event=public_event)

    rsvp = manager.rsvp(EventRSVP.RsvpStatus.YES, note="")

    assert rsvp.note == ""


def test_note_stored_when_flag_on(public_user: RevelUser, rsvp_event: Event) -> None:
    """The note is stored with the RSVP when the event accepts notes."""
    manager = EventManager(user=public_user, event=rsvp_event)

    rsvp = manager.rsvp(EventRSVP.RsvpStatus.YES, note="I am vegetarian")

    assert rsvp.note == "I am vegetarian"


def test_new_rsvp_overrides_note(public_user: RevelUser, rsvp_event: Event) -> None:
    """A later RSVP call replaces the stored note wholesale."""
    manager = EventManager(user=public_user, event=rsvp_event)
    manager.rsvp(EventRSVP.RsvpStatus.YES, note="first note")

    rsvp = manager.rsvp(EventRSVP.RsvpStatus.YES, note="second note")

    assert rsvp.note == "second note"


def test_rsvp_without_note_clears_existing(public_user: RevelUser, rsvp_event: Event) -> None:
    """An RSVP call without a note clears any stored note (override semantics)."""
    manager = EventManager(user=public_user, event=rsvp_event)
    manager.rsvp(EventRSVP.RsvpStatus.YES, note="a note")

    rsvp = manager.rsvp(EventRSVP.RsvpStatus.MAYBE)

    assert rsvp.note == ""
