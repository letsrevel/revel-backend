"""Tests for send_waitlist_offer_notification_task."""

import datetime as dt
import uuid
from unittest import mock

import pytest
from django.utils import timezone

from conftest import RevelUserFactory  # type: ignore[import-not-found]
from events.models import Event, WaitlistOffer
from events.tasks import send_waitlist_offer_notification_task

pytestmark = pytest.mark.django_db


# The task uses function-scoped imports to keep the events.tasks module loadable
# on the Celery worker without pulling notifications eagerly. Patch the signal at
# its source so the test does not rely on a module-level alias inside events.tasks.
_NOTIFICATION_REQUESTED_PATH = "notifications.signals.notification_requested.send"


def _make_offer(event: Event, user: object, is_cutoff: bool = False) -> WaitlistOffer:
    return WaitlistOffer.objects.create(
        event=event,
        user=user,
        expires_at=timezone.now() + dt.timedelta(hours=24),
        batch_id=uuid.uuid4(),
        is_cutoff_batch=is_cutoff,
    )


def test_dispatches_with_correct_notification_type_and_user(
    event: Event, revel_user_factory: RevelUserFactory
) -> None:
    u = revel_user_factory()
    offer = _make_offer(event, u)

    with mock.patch(_NOTIFICATION_REQUESTED_PATH) as mocked:
        result = send_waitlist_offer_notification_task(str(offer.id))

    assert result["status"] == "sent"
    assert mocked.call_count == 1
    kwargs = mocked.call_args.kwargs
    assert kwargs["notification_type"] == "waitlist_spot_available"
    assert kwargs["user"].id == u.id


def test_context_payload_has_required_fields(
    event: Event, revel_user_factory: RevelUserFactory
) -> None:
    u = revel_user_factory()
    offer = _make_offer(event, u)

    with mock.patch(_NOTIFICATION_REQUESTED_PATH) as mocked:
        send_waitlist_offer_notification_task(str(offer.id))

    ctx = mocked.call_args.kwargs["context"]
    required = {
        "event_id",
        "event_name",
        "event_start",
        "event_start_formatted",
        "event_url",
        "organization_id",
        "organization_name",
        "offer_id",
        "expires_at",
        "expires_at_formatted",
        "time_remaining_formatted",
        "is_cutoff_batch",
    }
    assert required.issubset(ctx.keys())
    assert ctx["event_id"] == str(event.id)
    assert ctx["offer_id"] == str(offer.id)
    assert ctx["is_cutoff_batch"] is False


def test_marks_notified_at(event: Event, revel_user_factory: RevelUserFactory) -> None:
    u = revel_user_factory()
    offer = _make_offer(event, u)

    with mock.patch(_NOTIFICATION_REQUESTED_PATH):
        send_waitlist_offer_notification_task(str(offer.id))

    offer.refresh_from_db()
    assert offer.notified_at is not None


def test_skips_non_pending_offer(event: Event, revel_user_factory: RevelUserFactory) -> None:
    u = revel_user_factory()
    offer = _make_offer(event, u)
    offer.status = WaitlistOffer.Status.EXPIRED
    offer.save(update_fields=["status"])

    with mock.patch(_NOTIFICATION_REQUESTED_PATH) as mocked:
        result = send_waitlist_offer_notification_task(str(offer.id))

    assert result["status"] == "skipped"
    mocked.assert_not_called()


def test_skips_missing_offer(event: Event, revel_user_factory: RevelUserFactory) -> None:
    """Unknown offer id is a no-op (returns skipped)."""
    bogus = str(uuid.uuid4())

    with mock.patch(_NOTIFICATION_REQUESTED_PATH) as mocked:
        result = send_waitlist_offer_notification_task(bogus)

    assert result == {"status": "skipped", "offer_id": bogus}
    mocked.assert_not_called()


def test_cutoff_batch_context_flag(event: Event, revel_user_factory: RevelUserFactory) -> None:
    u = revel_user_factory()
    offer = _make_offer(event, u, is_cutoff=True)

    with mock.patch(_NOTIFICATION_REQUESTED_PATH) as mocked:
        send_waitlist_offer_notification_task(str(offer.id))

    ctx = mocked.call_args.kwargs["context"]
    assert ctx["is_cutoff_batch"] is True
