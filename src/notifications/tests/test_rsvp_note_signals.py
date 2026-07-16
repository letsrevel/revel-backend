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


def test_cleared_note_fires_rsvp_updated_without_note(
    public_event: Event,
    member_user: RevelUser,
    regular_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """Clearing the note (same status) fires RSVP_UPDATED, but rsvp_note is absent from the context."""
    _make_rsvp(
        public_event,
        member_user,
        django_capture_on_commit_callbacks,
        status=EventRSVP.RsvpStatus.YES,
        note="something",
    )

    _make_rsvp(
        public_event,
        member_user,
        django_capture_on_commit_callbacks,
        status=EventRSVP.RsvpStatus.YES,
        note="",
    )

    notification = Notification.objects.filter(
        user=regular_user, notification_type=NotificationType.RSVP_UPDATED
    ).latest("created_at")
    assert "rsvp_note" not in notification.context
    assert notification.context["old_response"] == notification.context["new_response"]


def test_null_status_change_fires_rsvp_updated(
    public_event: Event,
    member_user: RevelUser,
    regular_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """A status change from NULL (status is nullable) to a real answer fires RSVP_UPDATED."""
    EventRSVP.objects.create(event=public_event, user=member_user)  # status defaults to None

    _make_rsvp(public_event, member_user, django_capture_on_commit_callbacks, status=EventRSVP.RsvpStatus.YES)

    notification = Notification.objects.filter(
        user=regular_user, notification_type=NotificationType.RSVP_UPDATED
    ).latest("created_at")
    assert notification.context["old_response"] is None
    assert notification.context["new_response"] == EventRSVP.RsvpStatus.YES


def test_double_save_in_one_transaction_fires_single_update(
    public_event: Event,
    member_user: RevelUser,
    regular_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """A changed save followed by an unchanged re-save of the same instance sends one notification.

    The pre_save hook stamps ``_old_status``/``_old_note`` on the in-memory
    instance; without post-dispatch cleanup the second save's on_commit
    callback would re-read the stale attributes and emit a duplicate.
    """
    rsvp = _make_rsvp(
        public_event,
        member_user,
        django_capture_on_commit_callbacks,
        status=EventRSVP.RsvpStatus.YES,
        note="",
    )

    with django_capture_on_commit_callbacks(execute=True):
        rsvp.status = EventRSVP.RsvpStatus.MAYBE
        rsvp.save()
        rsvp.save()  # unchanged re-save of the same in-memory instance

    assert Notification.objects.filter(user=regular_user, notification_type=NotificationType.RSVP_UPDATED).count() == 1


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
