"""Verify Tasks 15-18 wire enqueue/revoke into their respective code paths.

Like ``test_enqueue_wires.py``, these are wiring tests — they assert that the
correct helper (``enqueue_waitlist_processing`` or ``revoke_all_pending_offers``)
is invoked from each capacity-impacting code path. The full waitlist-processing
pipeline has its own dedicated tests.
"""

import datetime as dt
import uuid
from unittest import mock

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Event, EventRSVP, EventWaitList, WaitlistOffer
from events.service.event_manager.manager import EventManager
from events.service.waitlist_service import revoke_all_pending_offers

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_rsvp_event(event: Event, capacity: int = 5) -> None:
    """Reshape the generic ``event`` fixture into an open RSVP-only event."""
    event.end = event.start + dt.timedelta(hours=2)
    event.requires_ticket = False
    event.max_attendees = capacity
    event.waitlist_open = True
    event.waitlist_time_window = dt.timedelta(hours=24)
    event.save()


@pytest.fixture
def owner_jwt_client(organization_owner_user: RevelUser) -> Client:
    """Local API client for the organization owner (mirrors test_enqueue_wires)."""
    refresh = RefreshToken.for_user(organization_owner_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def member_jwt_client(member_user: RevelUser) -> Client:
    """Local API client for the member user."""
    refresh = RefreshToken.for_user(member_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Task 15 — RSVP YES -> non-YES enqueues (user-side via EventManager)
# ---------------------------------------------------------------------------


def test_user_rsvp_yes_to_no_enqueues(event: Event, revel_user_factory: RevelUserFactory) -> None:
    """Switching from YES to NO frees a seat — must trigger waitlist processing."""
    _open_rsvp_event(event)
    me = revel_user_factory()
    EventRSVP.objects.create(event=event, user=me, status=EventRSVP.RsvpStatus.YES)

    with mock.patch("events.service.event_manager.manager.enqueue_waitlist_processing") as mocked:
        EventManager(me, event).rsvp(EventRSVP.RsvpStatus.NO)

    mocked.assert_called_once_with(event.id)


def test_user_rsvp_yes_to_maybe_enqueues(event: Event, revel_user_factory: RevelUserFactory) -> None:
    """MAYBE also frees the seat (YES is the only seat-holding status for RSVPs)."""
    _open_rsvp_event(event)
    me = revel_user_factory()
    EventRSVP.objects.create(event=event, user=me, status=EventRSVP.RsvpStatus.YES)

    with mock.patch("events.service.event_manager.manager.enqueue_waitlist_processing") as mocked:
        EventManager(me, event).rsvp(EventRSVP.RsvpStatus.MAYBE)

    mocked.assert_called_once_with(event.id)


def test_user_first_no_does_not_enqueue(event: Event, revel_user_factory: RevelUserFactory) -> None:
    """A user RSVPing NO without a prior YES did not free a seat."""
    _open_rsvp_event(event)
    me = revel_user_factory()

    with mock.patch("events.service.event_manager.manager.enqueue_waitlist_processing") as mocked:
        EventManager(me, event).rsvp(EventRSVP.RsvpStatus.NO)

    mocked.assert_not_called()


def test_user_rsvp_yes_to_yes_does_not_enqueue(event: Event, revel_user_factory: RevelUserFactory) -> None:
    """Idempotent re-submit of YES — no transition, nothing to enqueue."""
    _open_rsvp_event(event)
    me = revel_user_factory()
    EventRSVP.objects.create(event=event, user=me, status=EventRSVP.RsvpStatus.YES)

    with mock.patch("events.service.event_manager.manager.enqueue_waitlist_processing") as mocked:
        EventManager(me, event).rsvp(EventRSVP.RsvpStatus.YES)

    mocked.assert_not_called()


# ---------------------------------------------------------------------------
# Task 15 — Admin RSVP endpoints
# ---------------------------------------------------------------------------


def test_admin_create_rsvp_yes_to_no_enqueues(
    owner_jwt_client: Client,
    event: Event,
    member_user: RevelUser,
) -> None:
    """``create_rsvp`` (PUT-like upsert) flipping YES to NO must enqueue."""
    _open_rsvp_event(event)
    EventRSVP.objects.create(event=event, user=member_user, status=EventRSVP.RsvpStatus.YES)

    url = reverse("api:create_rsvp", kwargs={"event_id": event.pk})
    payload = {"user_id": str(member_user.pk), "status": "no"}

    with mock.patch("events.controllers.event_admin.rsvps.enqueue_waitlist_processing") as mocked:
        response = owner_jwt_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    mocked.assert_called_once_with(event.id)


def test_admin_create_rsvp_no_prior_does_not_enqueue(
    owner_jwt_client: Client,
    event: Event,
    member_user: RevelUser,
) -> None:
    """First-time admin RSVP for a user did not free a seat."""
    _open_rsvp_event(event)
    url = reverse("api:create_rsvp", kwargs={"event_id": event.pk})
    payload = {"user_id": str(member_user.pk), "status": "no"}

    with mock.patch("events.controllers.event_admin.rsvps.enqueue_waitlist_processing") as mocked:
        response = owner_jwt_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    mocked.assert_not_called()


def test_admin_update_rsvp_yes_to_no_enqueues(
    owner_jwt_client: Client,
    event: Event,
    member_user: RevelUser,
) -> None:
    """``update_rsvp`` (PUT to a specific RSVP) flipping YES to NO must enqueue."""
    _open_rsvp_event(event)
    rsvp = EventRSVP.objects.create(event=event, user=member_user, status=EventRSVP.RsvpStatus.YES)

    url = reverse("api:update_rsvp", kwargs={"event_id": event.pk, "rsvp_id": rsvp.pk})
    payload = {"status": "no"}

    with mock.patch("events.controllers.event_admin.rsvps.enqueue_waitlist_processing") as mocked:
        response = owner_jwt_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    mocked.assert_called_once_with(event.id)


def test_admin_update_rsvp_no_to_yes_does_not_enqueue(
    owner_jwt_client: Client,
    event: Event,
    member_user: RevelUser,
) -> None:
    """Promoting NO -> YES takes a seat (not frees one) — no enqueue."""
    _open_rsvp_event(event)
    rsvp = EventRSVP.objects.create(event=event, user=member_user, status=EventRSVP.RsvpStatus.NO)

    url = reverse("api:update_rsvp", kwargs={"event_id": event.pk, "rsvp_id": rsvp.pk})
    payload = {"status": "yes"}

    with mock.patch("events.controllers.event_admin.rsvps.enqueue_waitlist_processing") as mocked:
        response = owner_jwt_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    mocked.assert_not_called()


# ---------------------------------------------------------------------------
# Task 16 — capacity increase via update_event
# ---------------------------------------------------------------------------


def test_update_event_capacity_increase_enqueues(
    owner_jwt_client: Client,
    event: Event,
) -> None:
    """Raising ``max_attendees`` to a larger value enqueues waitlist processing."""
    event.max_attendees = 5
    event.waitlist_open = True
    event.save(update_fields=["max_attendees", "waitlist_open"])

    url = reverse("api:edit_event", kwargs={"event_id": event.pk})
    payload = {"max_attendees": 10, "visibility": event.visibility}

    with mock.patch("events.controllers.event_admin.core.enqueue_waitlist_processing") as mocked:
        response = owner_jwt_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    mocked.assert_called_once_with(event.id)


def test_update_event_capacity_unchanged_does_not_enqueue(
    owner_jwt_client: Client,
    event: Event,
) -> None:
    """A no-op (re-submit same capacity) must not enqueue."""
    event.max_attendees = 5
    event.waitlist_open = True
    event.save(update_fields=["max_attendees", "waitlist_open"])

    url = reverse("api:edit_event", kwargs={"event_id": event.pk})
    payload = {"max_attendees": 5, "visibility": event.visibility}

    with mock.patch("events.controllers.event_admin.core.enqueue_waitlist_processing") as mocked:
        response = owner_jwt_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    mocked.assert_not_called()


def test_update_event_capacity_decrease_does_not_enqueue(
    owner_jwt_client: Client,
    event: Event,
) -> None:
    """Shrinking capacity is the opposite of freeing seats — no enqueue."""
    event.max_attendees = 10
    event.waitlist_open = True
    event.save(update_fields=["max_attendees", "waitlist_open"])

    url = reverse("api:edit_event", kwargs={"event_id": event.pk})
    payload = {"max_attendees": 5, "visibility": event.visibility}

    with mock.patch("events.controllers.event_admin.core.enqueue_waitlist_processing") as mocked:
        response = owner_jwt_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    mocked.assert_not_called()


# ---------------------------------------------------------------------------
# Task 17 — event status transitions + waitlist_open toggle
# ---------------------------------------------------------------------------


def test_update_event_status_cancelled_revokes_offers(
    owner_jwt_client: Client,
    event: Event,
) -> None:
    """Cancelling an event revokes all pending offers."""
    event.waitlist_open = True
    event.save(update_fields=["waitlist_open"])

    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": "cancelled"})

    with mock.patch("events.controllers.event_admin.core.revoke_all_pending_offers") as mocked:
        response = owner_jwt_client.post(url, data="{}", content_type="application/json")

    assert response.status_code == 200
    mocked.assert_called_once_with(event.id)


def test_update_event_status_uncancel_enqueues(
    owner_jwt_client: Client,
    event: Event,
) -> None:
    """Re-opening a previously-cancelled event re-creates real seats — enqueue."""
    event.status = Event.EventStatus.CANCELLED
    event.waitlist_open = True
    event.save(update_fields=["status", "waitlist_open"])

    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": "open"})

    with mock.patch("events.controllers.event_admin.core.enqueue_waitlist_processing") as mocked:
        response = owner_jwt_client.post(url, data="{}", content_type="application/json")

    assert response.status_code == 200
    mocked.assert_called_once_with(event.id)


def test_update_event_status_open_to_open_no_revoke_no_enqueue(
    owner_jwt_client: Client,
    event: Event,
) -> None:
    """Non-cancellation transitions do not touch waitlist offers."""
    event.status = Event.EventStatus.OPEN
    event.save(update_fields=["status"])

    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": "open"})

    with (
        mock.patch("events.controllers.event_admin.core.enqueue_waitlist_processing") as enqueue_mock,
        mock.patch("events.controllers.event_admin.core.revoke_all_pending_offers") as revoke_mock,
    ):
        response = owner_jwt_client.post(url, data="{}", content_type="application/json")

    assert response.status_code == 200
    enqueue_mock.assert_not_called()
    revoke_mock.assert_not_called()


def test_update_event_waitlist_open_true_to_false_revokes(
    owner_jwt_client: Client,
    event: Event,
) -> None:
    """Closing the waitlist via PUT revokes outstanding pending offers."""
    event.waitlist_open = True
    event.save(update_fields=["waitlist_open"])

    url = reverse("api:edit_event", kwargs={"event_id": event.pk})
    payload = {"waitlist_open": False, "visibility": event.visibility}

    with mock.patch("events.controllers.event_admin.core.revoke_all_pending_offers") as mocked:
        response = owner_jwt_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    mocked.assert_called_once_with(event.id)


def test_update_event_waitlist_open_false_to_true_does_not_revoke(
    owner_jwt_client: Client,
    event: Event,
) -> None:
    """Opening the waitlist does not revoke offers."""
    event.waitlist_open = False
    event.save(update_fields=["waitlist_open"])

    url = reverse("api:edit_event", kwargs={"event_id": event.pk})
    payload = {"waitlist_open": True, "visibility": event.visibility}

    with mock.patch("events.controllers.event_admin.core.revoke_all_pending_offers") as mocked:
        response = owner_jwt_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    mocked.assert_not_called()


# ---------------------------------------------------------------------------
# Task 17 — revoke_all_pending_offers helper unit test
# ---------------------------------------------------------------------------


def test_revoke_all_pending_offers_marks_pending_as_revoked(
    event: Event,
    revel_user_factory: RevelUserFactory,
) -> None:
    """Pending offers transition to REVOKED; other statuses are untouched."""
    pending_offers: list[WaitlistOffer] = []
    for _ in range(3):
        pending_offers.append(
            WaitlistOffer.objects.create(
                event=event,
                user=revel_user_factory(),
                expires_at=timezone.now() + dt.timedelta(hours=1),
                batch_id=uuid.uuid4(),
            )
        )

    # Already-claimed offer must remain untouched.
    claimed = WaitlistOffer.objects.create(
        event=event,
        user=revel_user_factory(),
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
    )
    claimed.status = WaitlistOffer.Status.CLAIMED
    claimed.save(update_fields=["status"])

    count = revoke_all_pending_offers(event.id)

    assert count == 3
    assert WaitlistOffer.objects.filter(event=event, status=WaitlistOffer.Status.REVOKED).count() == 3
    claimed.refresh_from_db()
    assert claimed.status == WaitlistOffer.Status.CLAIMED


def test_revoke_all_pending_offers_ignores_other_events(
    event: Event,
    public_event: Event,
    revel_user_factory: RevelUserFactory,
) -> None:
    """Revoking offers for one event must not touch another event's offers."""
    other_offer = WaitlistOffer.objects.create(
        event=public_event,
        user=revel_user_factory(),
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
    )
    WaitlistOffer.objects.create(
        event=event,
        user=revel_user_factory(),
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
    )

    count = revoke_all_pending_offers(event.id)

    assert count == 1
    other_offer.refresh_from_db()
    assert other_offer.status == WaitlistOffer.Status.PENDING


# ---------------------------------------------------------------------------
# Task 18 — leave_waitlist expires pending offer + enqueues next batch
# ---------------------------------------------------------------------------


def test_leave_waitlist_with_offer_expires_and_enqueues(
    member_jwt_client: Client,
    member_user: RevelUser,
    event: Event,
) -> None:
    """Leaving while holding a pending offer marks it EXPIRED and enqueues a refill."""
    _open_rsvp_event(event)
    EventWaitList.objects.create(event=event, user=member_user)
    offer = WaitlistOffer.objects.create(
        event=event,
        user=member_user,
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
    )

    url = reverse("api:leave_waitlist", kwargs={"event_id": event.pk})

    with mock.patch("events.controllers.event_public.attendance.enqueue_waitlist_processing") as mocked:
        response = member_jwt_client.delete(url)

    assert response.status_code == 200
    mocked.assert_called_once_with(event.id)

    offer.refresh_from_db()
    assert offer.status == WaitlistOffer.Status.EXPIRED
    assert not EventWaitList.objects.filter(event=event, user=member_user).exists()


def test_leave_waitlist_without_offer_does_not_enqueue(
    member_jwt_client: Client,
    member_user: RevelUser,
    event: Event,
) -> None:
    """No pending offer to release → no enqueue (waitlist row deletion alone)."""
    _open_rsvp_event(event)
    EventWaitList.objects.create(event=event, user=member_user)

    url = reverse("api:leave_waitlist", kwargs={"event_id": event.pk})

    with mock.patch("events.controllers.event_public.attendance.enqueue_waitlist_processing") as mocked:
        response = member_jwt_client.delete(url)

    assert response.status_code == 200
    mocked.assert_not_called()
    assert not EventWaitList.objects.filter(event=event, user=member_user).exists()


def test_leave_waitlist_with_time_expired_pending_offer_is_revoked_by_signal(
    member_jwt_client: Client,
    member_user: RevelUser,
    event: Event,
) -> None:
    """A time-expired-but-still-PENDING offer is REVOKED via the post_delete signal.

    ``leave_waitlist`` itself only flips offers that are PENDING **and** still
    in their time window (``expires_at > now``), so it does NOT call
    ``enqueue_waitlist_processing`` directly when the offer is past its expiry.
    The ``EventWaitList`` row delete then fires the ``post_delete`` signal in
    ``events/signals.py``, which finds the still-PENDING offer and marks it
    REVOKED (cleaner than leaving a zombie row for the sweeper to find).
    """
    _open_rsvp_event(event)
    EventWaitList.objects.create(event=event, user=member_user)
    expired = WaitlistOffer.objects.create(
        event=event,
        user=member_user,
        expires_at=timezone.now() - dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
    )

    url = reverse("api:leave_waitlist", kwargs={"event_id": event.pk})

    with mock.patch("events.controllers.event_public.attendance.enqueue_waitlist_processing") as controller_mock:
        response = member_jwt_client.delete(url)

    assert response.status_code == 200
    # leave_waitlist's own enqueue call is not made (its offer lookup filters
    # on `expires_at > now`); the signal's call is made via a different import
    # path and is not captured by this patch.
    controller_mock.assert_not_called()
    expired.refresh_from_db()
    assert expired.status == WaitlistOffer.Status.REVOKED
