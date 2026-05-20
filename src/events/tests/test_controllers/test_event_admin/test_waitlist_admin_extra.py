"""Tests for the reactivate, manual-create, and schema-enrichment waitlist features."""

import datetime as dt
import typing as t
import uuid
from unittest import mock

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from conftest import RevelUserFactory
from events.models import Event, EventWaitList, WaitlistOffer

pytestmark = pytest.mark.django_db


# ---------- Reactivate offer ----------


def _set_window(event: Event, window: dt.timedelta | None = dt.timedelta(hours=24)) -> None:
    event.waitlist_open = True
    event.waitlist_time_window = window
    event.save(update_fields=["waitlist_open", "waitlist_time_window"])


def test_reactivate_expired_offer_resets_status_and_expiry(
    organization_owner_client: Client,
    event: Event,
    revel_user_factory: RevelUserFactory,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    _set_window(event)
    user = revel_user_factory()
    offer = WaitlistOffer.objects.create(
        event=event,
        user=user,
        expires_at=timezone.now() - dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
        status=WaitlistOffer.Status.EXPIRED,
        notified_at=timezone.now() - dt.timedelta(hours=2),
    )
    url = reverse("api:reactivate_waitlist_offer", kwargs={"event_id": event.pk, "offer_id": offer.pk})

    with mock.patch("events.tasks.send_waitlist_offer_notification_task") as task_mock:
        with django_capture_on_commit_callbacks(execute=True):
            response = organization_owner_client.post(url, data=orjson.dumps({}), content_type="application/json")

    assert response.status_code == 200, response.content
    offer.refresh_from_db()
    assert offer.status == WaitlistOffer.Status.PENDING
    assert offer.notified_at is None
    assert offer.claimed_at is None
    # expires_at is now + 24h (within a few seconds)
    expected = timezone.now() + dt.timedelta(hours=24)
    assert abs((offer.expires_at - expected).total_seconds()) < 5
    task_mock.delay.assert_called_once_with(str(offer.id))


def test_reactivate_with_custom_expires_at(
    organization_owner_client: Client,
    event: Event,
    revel_user_factory: RevelUserFactory,
) -> None:
    _set_window(event)
    user = revel_user_factory()
    offer = WaitlistOffer.objects.create(
        event=event,
        user=user,
        expires_at=timezone.now() - dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
        status=WaitlistOffer.Status.REVOKED,
    )
    new_expiry = (timezone.now() + dt.timedelta(hours=6)).isoformat()
    url = reverse("api:reactivate_waitlist_offer", kwargs={"event_id": event.pk, "offer_id": offer.pk})

    with mock.patch("events.tasks.send_waitlist_offer_notification_task"):
        response = organization_owner_client.post(
            url, data=orjson.dumps({"expires_at": new_expiry}), content_type="application/json"
        )

    assert response.status_code == 200, response.content
    offer.refresh_from_db()
    assert offer.status == WaitlistOffer.Status.PENDING
    assert abs((offer.expires_at - dt.datetime.fromisoformat(new_expiry)).total_seconds()) < 1


def test_reactivate_already_pending_returns_404(
    organization_owner_client: Client,
    event: Event,
    revel_user_factory: RevelUserFactory,
) -> None:
    _set_window(event)
    offer = WaitlistOffer.objects.create(
        event=event,
        user=revel_user_factory(),
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
        status=WaitlistOffer.Status.PENDING,
    )
    url = reverse("api:reactivate_waitlist_offer", kwargs={"event_id": event.pk, "offer_id": offer.pk})
    response = organization_owner_client.post(url, data=orjson.dumps({}), content_type="application/json")
    assert response.status_code == 404


def test_reactivate_already_claimed_returns_404(
    organization_owner_client: Client,
    event: Event,
    revel_user_factory: RevelUserFactory,
) -> None:
    """Only EXPIRED / REVOKED offers may be reactivated; CLAIMED is not eligible."""
    _set_window(event)
    offer = WaitlistOffer.objects.create(
        event=event,
        user=revel_user_factory(),
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
        status=WaitlistOffer.Status.CLAIMED,
        claimed_at=timezone.now(),
    )
    url = reverse("api:reactivate_waitlist_offer", kwargs={"event_id": event.pk, "offer_id": offer.pk})
    response = organization_owner_client.post(url, data=orjson.dumps({}), content_type="application/json")
    assert response.status_code == 404


def test_reactivate_conflicts_with_other_pending_offer(
    organization_owner_client: Client,
    event: Event,
    revel_user_factory: RevelUserFactory,
) -> None:
    _set_window(event)
    user = revel_user_factory()
    expired = WaitlistOffer.objects.create(
        event=event,
        user=user,
        expires_at=timezone.now() - dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
        status=WaitlistOffer.Status.EXPIRED,
    )
    WaitlistOffer.objects.create(
        event=event,
        user=user,
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
        status=WaitlistOffer.Status.PENDING,
    )
    url = reverse("api:reactivate_waitlist_offer", kwargs={"event_id": event.pk, "offer_id": expired.pk})
    response = organization_owner_client.post(url, data=orjson.dumps({}), content_type="application/json")
    assert response.status_code == 409


def test_reactivate_without_window_returns_400(
    organization_owner_client: Client,
    event: Event,
    revel_user_factory: RevelUserFactory,
) -> None:
    # Window is None by default; ensure it stays None
    assert event.waitlist_time_window is None
    offer = WaitlistOffer.objects.create(
        event=event,
        user=revel_user_factory(),
        expires_at=timezone.now() - dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
        status=WaitlistOffer.Status.EXPIRED,
    )
    url = reverse("api:reactivate_waitlist_offer", kwargs={"event_id": event.pk, "offer_id": offer.pk})
    response = organization_owner_client.post(url, data=orjson.dumps({}), content_type="application/json")
    assert response.status_code == 400


def test_reactivate_dispatches_notification(
    organization_owner_client: Client,
    event: Event,
    revel_user_factory: RevelUserFactory,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    _set_window(event)
    offer = WaitlistOffer.objects.create(
        event=event,
        user=revel_user_factory(),
        expires_at=timezone.now() - dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
        status=WaitlistOffer.Status.EXPIRED,
    )
    url = reverse("api:reactivate_waitlist_offer", kwargs={"event_id": event.pk, "offer_id": offer.pk})
    with mock.patch("events.tasks.send_waitlist_offer_notification_task") as task_mock:
        with django_capture_on_commit_callbacks(execute=True):
            response = organization_owner_client.post(url, data=orjson.dumps({}), content_type="application/json")
    assert response.status_code == 200
    task_mock.delay.assert_called_once_with(str(offer.id))


def test_reactivate_does_not_dispatch_before_commit(
    organization_owner_client: Client,
    event: Event,
    revel_user_factory: RevelUserFactory,
) -> None:
    """Notification task must be scheduled via on_commit, not fired immediately."""
    _set_window(event)
    offer = WaitlistOffer.objects.create(
        event=event,
        user=revel_user_factory(),
        expires_at=timezone.now() - dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
        status=WaitlistOffer.Status.EXPIRED,
    )
    url = reverse("api:reactivate_waitlist_offer", kwargs={"event_id": event.pk, "offer_id": offer.pk})
    # Without django_capture_on_commit_callbacks, the test runs inside an
    # outer transaction that never commits, so the on_commit hook never fires.
    with mock.patch("events.tasks.send_waitlist_offer_notification_task") as task_mock:
        response = organization_owner_client.post(url, data=orjson.dumps({}), content_type="application/json")
    assert response.status_code == 200
    task_mock.delay.assert_not_called()


def test_reactivate_race_loss_returns_409(
    organization_owner_client: Client,
    event: Event,
    revel_user_factory: RevelUserFactory,
) -> None:
    """If the unique-pending constraint trips on save, the endpoint returns 409 (not 500)."""
    from django.db import IntegrityError

    _set_window(event)
    offer = WaitlistOffer.objects.create(
        event=event,
        user=revel_user_factory(),
        expires_at=timezone.now() - dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
        status=WaitlistOffer.Status.EXPIRED,
    )
    url = reverse("api:reactivate_waitlist_offer", kwargs={"event_id": event.pk, "offer_id": offer.pk})
    with mock.patch(
        "events.controllers.event_admin.waitlist_offers.models.WaitlistOffer.save",
        side_effect=IntegrityError("duplicate pending"),
    ):
        response = organization_owner_client.post(url, data=orjson.dumps({}), content_type="application/json")
    assert response.status_code == 409


# ---------- Manual create offer ----------


def test_create_offer_from_entry(
    organization_owner_client: Client,
    event: Event,
    revel_user_factory: RevelUserFactory,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    _set_window(event)
    user = revel_user_factory()
    entry = EventWaitList.objects.create(event=event, user=user)
    url = reverse("api:create_waitlist_offer", kwargs={"event_id": event.pk})
    with mock.patch("events.tasks.send_waitlist_offer_notification_task") as task_mock:
        with django_capture_on_commit_callbacks(execute=True):
            response = organization_owner_client.post(
                url,
                data=orjson.dumps({"waitlist_entry_id": str(entry.pk)}),
                content_type="application/json",
            )
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["status"] == WaitlistOffer.Status.PENDING.value
    offer = WaitlistOffer.objects.get(pk=body["id"])
    assert offer.user_id == user.id
    assert offer.is_cutoff_batch is False
    expected = timezone.now() + dt.timedelta(hours=24)
    assert abs((offer.expires_at - expected).total_seconds()) < 5
    task_mock.delay.assert_called_once_with(str(offer.id))


def test_create_offer_with_custom_expires_at(
    organization_owner_client: Client,
    event: Event,
    revel_user_factory: RevelUserFactory,
) -> None:
    _set_window(event)
    user = revel_user_factory()
    entry = EventWaitList.objects.create(event=event, user=user)
    new_expiry = (timezone.now() + dt.timedelta(hours=3)).isoformat()
    url = reverse("api:create_waitlist_offer", kwargs={"event_id": event.pk})
    with mock.patch("events.tasks.send_waitlist_offer_notification_task"):
        response = organization_owner_client.post(
            url,
            data=orjson.dumps({"waitlist_entry_id": str(entry.pk), "expires_at": new_expiry}),
            content_type="application/json",
        )
    assert response.status_code == 201, response.content
    body = response.json()
    offer = WaitlistOffer.objects.get(pk=body["id"])
    assert abs((offer.expires_at - dt.datetime.fromisoformat(new_expiry)).total_seconds()) < 1


def test_create_offer_when_user_already_has_pending_returns_409(
    organization_owner_client: Client,
    event: Event,
    revel_user_factory: RevelUserFactory,
) -> None:
    _set_window(event)
    user = revel_user_factory()
    entry = EventWaitList.objects.create(event=event, user=user)
    WaitlistOffer.objects.create(
        event=event,
        user=user,
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
        status=WaitlistOffer.Status.PENDING,
    )
    url = reverse("api:create_waitlist_offer", kwargs={"event_id": event.pk})
    response = organization_owner_client.post(
        url,
        data=orjson.dumps({"waitlist_entry_id": str(entry.pk)}),
        content_type="application/json",
    )
    assert response.status_code == 409


def test_create_offer_without_window_returns_400(
    organization_owner_client: Client,
    event: Event,
    revel_user_factory: RevelUserFactory,
) -> None:
    assert event.waitlist_time_window is None
    user = revel_user_factory()
    entry = EventWaitList.objects.create(event=event, user=user)
    url = reverse("api:create_waitlist_offer", kwargs={"event_id": event.pk})
    response = organization_owner_client.post(
        url,
        data=orjson.dumps({"waitlist_entry_id": str(entry.pk)}),
        content_type="application/json",
    )
    assert response.status_code == 400


def test_create_offer_race_loss_returns_409(
    organization_owner_client: Client,
    event: Event,
    revel_user_factory: RevelUserFactory,
) -> None:
    """If a concurrent create wins the unique-pending race, return 409 (not 500)."""
    from django.db import IntegrityError

    _set_window(event)
    user = revel_user_factory()
    entry = EventWaitList.objects.create(event=event, user=user)
    url = reverse("api:create_waitlist_offer", kwargs={"event_id": event.pk})
    with mock.patch(
        "events.controllers.event_admin.waitlist_offers.models.WaitlistOffer.objects.create",
        side_effect=IntegrityError("duplicate pending"),
    ):
        response = organization_owner_client.post(
            url,
            data=orjson.dumps({"waitlist_entry_id": str(entry.pk)}),
            content_type="application/json",
        )
    assert response.status_code == 409


def test_create_offer_with_unknown_entry_returns_404(
    organization_owner_client: Client,
    event: Event,
) -> None:
    _set_window(event)
    url = reverse("api:create_waitlist_offer", kwargs={"event_id": event.pk})
    response = organization_owner_client.post(
        url,
        data=orjson.dumps({"waitlist_entry_id": str(uuid.uuid4())}),
        content_type="application/json",
    )
    assert response.status_code == 404


def test_create_offer_dispatches_notification(
    organization_owner_client: Client,
    event: Event,
    revel_user_factory: RevelUserFactory,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    _set_window(event)
    user = revel_user_factory()
    entry = EventWaitList.objects.create(event=event, user=user)
    url = reverse("api:create_waitlist_offer", kwargs={"event_id": event.pk})
    with mock.patch("events.tasks.send_waitlist_offer_notification_task") as task_mock:
        with django_capture_on_commit_callbacks(execute=True):
            response = organization_owner_client.post(
                url,
                data=orjson.dumps({"waitlist_entry_id": str(entry.pk)}),
                content_type="application/json",
            )
    assert response.status_code == 201
    body = response.json()
    task_mock.delay.assert_called_once_with(body["id"])


# ---------- Schema enrichment on list_waitlist ----------


def test_list_waitlist_includes_current_offer(
    organization_owner_client: Client,
    event: Event,
    revel_user_factory: RevelUserFactory,
) -> None:
    """Entries with a pending offer expose it; entries without one return null."""
    _set_window(event)
    user_with_offer = revel_user_factory()
    user_without_offer = revel_user_factory()
    entry_with_offer = EventWaitList.objects.create(event=event, user=user_with_offer)
    EventWaitList.objects.create(event=event, user=user_without_offer)
    offer = WaitlistOffer.objects.create(
        event=event,
        user=user_with_offer,
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
        status=WaitlistOffer.Status.PENDING,
    )

    url = reverse("api:list_waitlist", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url)
    assert response.status_code == 200, response.content
    body = response.json()
    items = body.get("items") or body.get("results") or []
    by_id = {item["id"]: item for item in items}
    assert by_id[str(entry_with_offer.id)]["current_offer"] is not None
    assert by_id[str(entry_with_offer.id)]["current_offer"]["id"] == str(offer.id)
    # Find the entry without an offer
    other = [item for item in items if item["id"] != str(entry_with_offer.id)][0]
    assert other["current_offer"] is None


def test_list_waitlist_hides_time_expired_pending_offer(
    organization_owner_client: Client,
    event: Event,
    revel_user_factory: RevelUserFactory,
) -> None:
    """A PENDING offer whose ``expires_at`` has passed is not exposed to admins.

    The hourly sweeper would normally transition it to EXPIRED, but the
    resolver filters on ``expires_at > now`` so a zombie row doesn't
    mislead admins into thinking the user can still claim.
    """
    _set_window(event)
    user_with_zombie = revel_user_factory()
    entry = EventWaitList.objects.create(event=event, user=user_with_zombie)
    WaitlistOffer.objects.create(
        event=event,
        user=user_with_zombie,
        expires_at=timezone.now() - dt.timedelta(minutes=5),
        batch_id=uuid.uuid4(),
        status=WaitlistOffer.Status.PENDING,
    )

    url = reverse("api:list_waitlist", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url)
    assert response.status_code == 200, response.content
    body = response.json()
    items = body.get("items") or body.get("results") or []
    by_id = {item["id"]: item for item in items}
    assert by_id[str(entry.id)]["current_offer"] is None
