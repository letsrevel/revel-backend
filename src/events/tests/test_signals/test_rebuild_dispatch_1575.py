"""Attendee rebuild dispatches are independent of the other commit callbacks (#1575).

A broker failure in a plain ``transaction.on_commit`` came out after the commit: the request answered 500 although
its data was stored, and the later callbacks (for an RSVP, the staff notification) did not run.
"""

import typing as t
from unittest.mock import patch

import pytest

from accounts.models import RevelUser
from events.models import Event, EventInvitation, EventRSVP

pytestmark = pytest.mark.django_db

DELAY = "events.tasks.attendees.build_attendee_visibility_flags.delay"  # the shared task object


def test_an_rsvp_save_survives_a_broker_failure_and_still_notifies_staff(
    event: Event, member_user: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    with (
        patch(DELAY, side_effect=ConnectionError("broker down")),
        patch("notifications.signals.rsvp._send_rsvp_confirmation_notifications") as notify,
        patch("common.utils.logger") as log,
        django_capture_on_commit_callbacks(execute=True),
    ):
        EventRSVP.objects.create(event=event, user=member_user, status=EventRSVP.RsvpStatus.YES)

    notify.assert_called_once()
    assert [c.args[0] for c in log.error.call_args_list] == ["visibility_flags_dispatch_failed"]


def test_an_rsvp_save_dispatches_the_rebuild_once(
    event: Event, member_user: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    with (
        patch(DELAY) as delay,
        patch("notifications.signals.rsvp._send_rsvp_confirmation_notifications"),
        django_capture_on_commit_callbacks(execute=True),
    ):
        EventRSVP.objects.create(event=event, user=member_user, status=EventRSVP.RsvpStatus.YES)

    delay.assert_called_once_with(str(event.pk))


def test_an_invitation_save_survives_a_broker_failure(
    event: Event, member_user: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    with (
        patch(DELAY, side_effect=ConnectionError("broker down")),
        django_capture_on_commit_callbacks(execute=True) as callbacks,
    ):
        EventInvitation.objects.create(event=event, user=member_user)

    assert callbacks  # the callbacks ran, and none raised


def test_one_failed_dispatch_does_not_skip_the_other_events(
    event: Event, member_user: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    """The preferences path dispatches one rebuild per event: a failure for one does not skip the next."""
    from events.service.user_preferences_service import trigger_visibility_flags_for_user

    other = Event.objects.create(organization=event.organization, name="Other", slug="other-1575", start=event.start)
    for target in (event, other):
        EventRSVP.objects.create(event=target, user=member_user, status=EventRSVP.RsvpStatus.YES)
    with (
        patch(DELAY, side_effect=[ConnectionError("broker down"), None]) as delay,
        django_capture_on_commit_callbacks(execute=True),
    ):
        trigger_visibility_flags_for_user(member_user.id)

    assert delay.call_count == 2
