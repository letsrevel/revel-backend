"""Tests for the RSVP note in staff RSVP notifications."""

import typing as t

import pytest

from accounts.models import RevelUser
from events.models import Event, EventRSVP
from notifications.enums import NotificationType
from notifications.models import Notification

pytestmark = pytest.mark.django_db


def _make_rsvp(event: Event, user: RevelUser, django_capture_on_commit_callbacks: t.Any, **kwargs: t.Any) -> EventRSVP:
    """Create/update an RSVP, executing on_commit callbacks so notifications fire synchronously."""
    with django_capture_on_commit_callbacks(execute=True):
        rsvp, _ = EventRSVP.objects.update_or_create(event=event, user=user, defaults=kwargs)
    return rsvp


def test_confirmation_context_contains_note(
    public_event: Event,
    member_user: RevelUser,
    regular_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """RSVP creation with a note puts rsvp_note into the staff RSVP_CONFIRMATION context."""
    _make_rsvp(
        public_event,
        member_user,
        django_capture_on_commit_callbacks,
        status=EventRSVP.RsvpStatus.YES,
        note="I bring dessert",
    )

    notification = Notification.objects.filter(
        user=regular_user, notification_type=NotificationType.RSVP_CONFIRMATION
    ).latest("created_at")
    assert notification.context["rsvp_note"] == "I bring dessert"


def test_note_only_change_fires_rsvp_updated(
    public_event: Event,
    member_user: RevelUser,
    regular_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """Re-saving with the same status but a new note fires RSVP_UPDATED with the note."""
    _make_rsvp(
        public_event,
        member_user,
        django_capture_on_commit_callbacks,
        status=EventRSVP.RsvpStatus.YES,
        note="old",
    )

    _make_rsvp(
        public_event,
        member_user,
        django_capture_on_commit_callbacks,
        status=EventRSVP.RsvpStatus.YES,
        note="new note",
    )

    notification = Notification.objects.filter(
        user=regular_user, notification_type=NotificationType.RSVP_UPDATED
    ).latest("created_at")
    assert notification.context["rsvp_note"] == "new note"
    assert notification.context["old_response"] == notification.context["new_response"]


def test_unchanged_resave_fires_nothing(
    public_event: Event,
    member_user: RevelUser,
    regular_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """Same status + same note = no RSVP_UPDATED notification."""
    _make_rsvp(
        public_event,
        member_user,
        django_capture_on_commit_callbacks,
        status=EventRSVP.RsvpStatus.YES,
        note="same",
    )

    _make_rsvp(
        public_event,
        member_user,
        django_capture_on_commit_callbacks,
        status=EventRSVP.RsvpStatus.YES,
        note="same",
    )

    assert not Notification.objects.filter(user=regular_user, notification_type=NotificationType.RSVP_UPDATED).exists()
