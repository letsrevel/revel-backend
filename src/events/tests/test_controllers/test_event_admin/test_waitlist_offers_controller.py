"""Endpoint tests for EventAdminWaitlistOffersController."""

import datetime as dt
import uuid

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from conftest import RevelUserFactory
from events.models import Event, WaitlistOffer

pytestmark = pytest.mark.django_db


# --- GET /event-admin/{event_id}/waitlist-settings ---


def test_get_waitlist_settings_by_owner(organization_owner_client: Client, event: Event) -> None:
    url = reverse("api:get_waitlist_settings", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url)
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["waitlist_open"] is False
    assert body["waitlist_batch_size"] == 0
    assert body["waitlist_lottery_mode"] is False


def test_get_waitlist_settings_unauthenticated(client: Client, event: Event) -> None:
    url = reverse("api:get_waitlist_settings", kwargs={"event_id": event.pk})
    response = client.get(url)
    assert response.status_code == 401


def test_get_waitlist_settings_nonowner_denied(nonmember_client: Client, event: Event) -> None:
    url = reverse("api:get_waitlist_settings", kwargs={"event_id": event.pk})
    response = nonmember_client.get(url)
    # nonmember has no access -> 403 from EventPermission
    assert response.status_code == 403


# --- PATCH /event-admin/{event_id}/waitlist-settings ---


def test_patch_waitlist_settings_updates_fields(organization_owner_client: Client, event: Event) -> None:
    url = reverse("api:update_waitlist_settings", kwargs={"event_id": event.pk})
    payload = {
        "waitlist_open": True,
        "waitlist_time_window": "PT24H",
        "waitlist_batch_size": 5,
        "waitlist_lottery_mode": True,
    }
    response = organization_owner_client.patch(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 200, response.content
    event.refresh_from_db()
    assert event.waitlist_open is True
    assert event.waitlist_batch_size == 5
    assert event.waitlist_time_window == dt.timedelta(hours=24)
    assert event.waitlist_lottery_mode is True


def test_patch_waitlist_settings_empty_payload_is_noop(organization_owner_client: Client, event: Event) -> None:
    url = reverse("api:update_waitlist_settings", kwargs={"event_id": event.pk})
    response = organization_owner_client.patch(url, data=orjson.dumps({}), content_type="application/json")
    assert response.status_code == 200, response.content
    event.refresh_from_db()
    assert event.waitlist_open is False


def test_patch_waitlist_settings_closing_revokes_pending_offers(
    organization_owner_client: Client,
    event: Event,
    revel_user_factory: RevelUserFactory,
) -> None:
    event.waitlist_open = True
    event.save(update_fields=["waitlist_open"])
    pending = WaitlistOffer.objects.create(
        event=event,
        user=revel_user_factory(),
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
    )

    url = reverse("api:update_waitlist_settings", kwargs={"event_id": event.pk})
    response = organization_owner_client.patch(
        url,
        data=orjson.dumps({"waitlist_open": False}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    pending.refresh_from_db()
    assert pending.status == WaitlistOffer.WaitlistOfferStatus.REVOKED


def test_patch_waitlist_settings_invalid_window_returns_validation_error(
    organization_owner_client: Client, event: Event
) -> None:
    url = reverse("api:update_waitlist_settings", kwargs={"event_id": event.pk})
    # 30 minutes < the 1-hour minimum enforced by Event.clean()
    response = organization_owner_client.patch(
        url,
        data=orjson.dumps({"waitlist_time_window": "PT30M"}),
        content_type="application/json",
    )
    assert response.status_code in (400, 422)


# --- GET /event-admin/{event_id}/waitlist-offers ---


def test_list_waitlist_offers_returns_paginated(
    organization_owner_client: Client,
    event: Event,
    revel_user_factory: RevelUserFactory,
) -> None:
    for _ in range(3):
        WaitlistOffer.objects.create(
            event=event,
            user=revel_user_factory(),
            expires_at=timezone.now() + dt.timedelta(hours=1),
            batch_id=uuid.uuid4(),
        )
    url = reverse("api:list_waitlist_offers", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url)
    assert response.status_code == 200, response.content
    body = response.json()
    items = body.get("items") or body.get("results") or []
    assert len(items) == 3


def test_list_waitlist_offers_returns_nested_user(
    organization_owner_client: Client,
    event: Event,
    revel_user_factory: RevelUserFactory,
) -> None:
    """``user`` is exposed as a nested object so the admin UI can render name + email."""
    user = revel_user_factory(first_name="Ada", last_name="Lovelace", email="ada@example.com")
    WaitlistOffer.objects.create(
        event=event,
        user=user,
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
    )
    url = reverse("api:list_waitlist_offers", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url)
    assert response.status_code == 200, response.content
    items = response.json().get("items") or []
    assert len(items) == 1
    user_payload = items[0]["user"]
    assert isinstance(user_payload, dict)
    assert user_payload["id"] == str(user.id)
    assert user_payload["email"] == "ada@example.com"
    assert user_payload["first_name"] == "Ada"
    assert user_payload["last_name"] == "Lovelace"


def test_list_waitlist_offers_filter_by_status(
    organization_owner_client: Client,
    event: Event,
    revel_user_factory: RevelUserFactory,
) -> None:
    WaitlistOffer.objects.create(
        event=event,
        user=revel_user_factory(),
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
        status=WaitlistOffer.WaitlistOfferStatus.PENDING,
    )
    WaitlistOffer.objects.create(
        event=event,
        user=revel_user_factory(),
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
        status=WaitlistOffer.WaitlistOfferStatus.CLAIMED,
    )
    url = reverse("api:list_waitlist_offers", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url, {"status": WaitlistOffer.WaitlistOfferStatus.PENDING.value})
    assert response.status_code == 200, response.content
    body = response.json()
    items = body.get("items") or body.get("results") or []
    assert len(items) == 1
    assert items[0]["status"] == WaitlistOffer.WaitlistOfferStatus.PENDING.value


# --- POST /event-admin/{event_id}/waitlist-offers/{offer_id}/revoke ---


def test_revoke_waitlist_offer_marks_revoked(
    organization_owner_client: Client,
    event: Event,
    revel_user_factory: RevelUserFactory,
) -> None:
    offer = WaitlistOffer.objects.create(
        event=event,
        user=revel_user_factory(),
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
    )
    url = reverse(
        "api:revoke_waitlist_offer",
        kwargs={"event_id": event.pk, "offer_id": offer.pk},
    )
    response = organization_owner_client.post(url)
    assert response.status_code == 200, response.content
    offer.refresh_from_db()
    assert offer.status == WaitlistOffer.WaitlistOfferStatus.REVOKED


def test_revoke_non_pending_offer_returns_404(
    organization_owner_client: Client,
    event: Event,
    revel_user_factory: RevelUserFactory,
) -> None:
    offer = WaitlistOffer.objects.create(
        event=event,
        user=revel_user_factory(),
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
        status=WaitlistOffer.WaitlistOfferStatus.CLAIMED,
    )
    url = reverse(
        "api:revoke_waitlist_offer",
        kwargs={"event_id": event.pk, "offer_id": offer.pk},
    )
    response = organization_owner_client.post(url)
    assert response.status_code == 404


def test_revoke_unknown_offer_returns_404(organization_owner_client: Client, event: Event) -> None:
    url = reverse(
        "api:revoke_waitlist_offer",
        kwargs={"event_id": event.pk, "offer_id": uuid.uuid4()},
    )
    response = organization_owner_client.post(url)
    assert response.status_code == 404
