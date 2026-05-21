"""Unit tests for ``events.service.event_update_service``.

These exercise the service layer directly (no HTTP) to verify:

- ``update_event`` triggers waitlist processing on capacity bumps.
- ``update_event`` revokes pending offers when ``waitlist_open`` flips
  True -> False.
- ``update_event`` flips ``is_modified=True`` on a real diff for a
  series occurrence, and leaves it alone on a no-op PUT.
- ``update_status`` revokes / enqueues correctly for CANCELLED transitions.
- ``update_slug`` raises ``SlugAlreadyExistsError`` on collision.
"""

from unittest import mock

import pytest

from events.models import Event, Organization
from events.schema import EventEditSchema
from events.service import event_service
from events.service.event_update_service import SlugAlreadyExistsError

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# update_event — waitlist side effects
# ---------------------------------------------------------------------------


def test_update_event_capacity_bump_enqueues_waitlist(event: Event) -> None:
    """Increasing ``max_attendees`` enqueues a waitlist pass."""
    event.max_attendees = 5
    event.save(update_fields=["max_attendees"])

    payload = EventEditSchema.model_validate({"max_attendees": 10, "visibility": event.visibility})

    with mock.patch("events.service.event_update_service.enqueue_waitlist_processing") as enqueue_mock:
        updated = event_service.update_event(event, payload)

    enqueue_mock.assert_called_once_with(updated.id)
    assert updated.max_attendees == 10


def test_update_event_capacity_unchanged_does_not_enqueue(event: Event) -> None:
    """Re-submitting the same capacity must not trigger processing."""
    event.max_attendees = 5
    event.save(update_fields=["max_attendees"])

    payload = EventEditSchema.model_validate({"max_attendees": 5, "visibility": event.visibility})

    with mock.patch("events.service.event_update_service.enqueue_waitlist_processing") as enqueue_mock:
        event_service.update_event(event, payload)

    enqueue_mock.assert_not_called()


def test_update_event_capacity_decrease_does_not_enqueue(event: Event) -> None:
    """Shrinking capacity is the opposite of freeing seats — no enqueue."""
    event.max_attendees = 10
    event.save(update_fields=["max_attendees"])

    payload = EventEditSchema.model_validate({"max_attendees": 5, "visibility": event.visibility})

    with mock.patch("events.service.event_update_service.enqueue_waitlist_processing") as enqueue_mock:
        event_service.update_event(event, payload)

    enqueue_mock.assert_not_called()


def test_update_event_waitlist_close_revokes_pending_offers(event: Event) -> None:
    """Flipping ``waitlist_open`` True -> False revokes pending offers."""
    event.waitlist_open = True
    event.save(update_fields=["waitlist_open"])

    payload = EventEditSchema.model_validate({"waitlist_open": False, "visibility": event.visibility})

    with mock.patch("events.service.event_update_service.revoke_all_pending_offers") as revoke_mock:
        updated = event_service.update_event(event, payload)

    revoke_mock.assert_called_once_with(updated.id)
    assert updated.waitlist_open is False


def test_update_event_waitlist_open_false_to_true_does_not_revoke(event: Event) -> None:
    """Opening the waitlist must not revoke offers."""
    event.waitlist_open = False
    event.save(update_fields=["waitlist_open"])

    payload = EventEditSchema.model_validate({"waitlist_open": True, "visibility": event.visibility})

    with mock.patch("events.service.event_update_service.revoke_all_pending_offers") as revoke_mock:
        event_service.update_event(event, payload)

    revoke_mock.assert_not_called()


# ---------------------------------------------------------------------------
# update_event — occurrence is_modified handling
# ---------------------------------------------------------------------------


def test_update_event_real_change_marks_occurrence_modified(event: Event) -> None:
    """A genuine field change flips ``is_modified`` on a series occurrence."""
    event.occurrence_index = 2
    event.is_modified = False
    event.save(update_fields=["occurrence_index", "is_modified"])

    payload = EventEditSchema.model_validate({"name": "Genuinely Changed", "visibility": event.visibility})
    updated = event_service.update_event(event, payload)

    assert updated.is_modified is True
    assert updated.name == "Genuinely Changed"


def test_update_event_noop_does_not_mark_occurrence_modified(event: Event) -> None:
    """A no-op PUT (re-submitting the same value) must NOT flip ``is_modified``."""
    event.occurrence_index = 2
    event.is_modified = False
    event.name = "Stable Name"
    event.save(update_fields=["occurrence_index", "is_modified", "name"])

    payload = EventEditSchema.model_validate({"name": "Stable Name", "visibility": event.visibility})
    updated = event_service.update_event(event, payload)

    assert updated.is_modified is False


# ---------------------------------------------------------------------------
# update_status — waitlist side effects
# ---------------------------------------------------------------------------


def test_update_status_cancelled_revokes_offers(event: Event) -> None:
    """Cancelling an event revokes all pending offers."""
    event.status = Event.EventStatus.OPEN
    event.save(update_fields=["status"])

    with mock.patch("events.service.event_update_service.revoke_all_pending_offers") as revoke_mock:
        updated = event_service.update_status(event, Event.EventStatus.CANCELLED)

    revoke_mock.assert_called_once_with(updated.id)
    assert updated.status == Event.EventStatus.CANCELLED


def test_update_status_uncancel_enqueues_waitlist(event: Event) -> None:
    """Un-cancelling enqueues waitlist processing."""
    event.status = Event.EventStatus.CANCELLED
    event.save(update_fields=["status"])

    with mock.patch("events.service.event_update_service.enqueue_waitlist_processing") as enqueue_mock:
        updated = event_service.update_status(event, Event.EventStatus.OPEN)

    enqueue_mock.assert_called_once_with(updated.id)
    assert updated.status == Event.EventStatus.OPEN


def test_update_status_open_to_open_is_noop_for_side_effects(event: Event) -> None:
    """Non-cancellation transitions do not touch the waitlist."""
    event.status = Event.EventStatus.OPEN
    event.save(update_fields=["status"])

    with (
        mock.patch("events.service.event_update_service.enqueue_waitlist_processing") as enqueue_mock,
        mock.patch("events.service.event_update_service.revoke_all_pending_offers") as revoke_mock,
    ):
        event_service.update_status(event, Event.EventStatus.OPEN)

    enqueue_mock.assert_not_called()
    revoke_mock.assert_not_called()


# ---------------------------------------------------------------------------
# update_slug — uniqueness
# ---------------------------------------------------------------------------


def test_update_slug_changes_slug(event: Event) -> None:
    """A free slug is applied and persisted."""
    updated = event_service.update_slug(event, "brand-new-slug")
    updated.refresh_from_db()
    assert updated.slug == "brand-new-slug"


def test_update_slug_raises_on_collision(event: Event, organization: Organization) -> None:
    """A slug already used by another event in the same org raises."""
    Event.objects.create(
        organization=organization,
        name="Other",
        slug="taken-slug",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        start=event.start,
        status="open",
        requires_ticket=True,
    )

    with pytest.raises(SlugAlreadyExistsError) as exc_info:
        event_service.update_slug(event, "taken-slug")

    assert exc_info.value.slug == "taken-slug"

    event.refresh_from_db()
    # Slug unchanged on failure
    assert event.slug != "taken-slug"


def test_update_slug_same_slug_no_collision(event: Event) -> None:
    """Re-setting the same slug on the same event is a no-op (not a collision)."""
    original = event.slug
    updated = event_service.update_slug(event, original)
    assert updated.slug == original
