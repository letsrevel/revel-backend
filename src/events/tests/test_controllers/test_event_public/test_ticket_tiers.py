"""Public ticket-tier listing schema tests."""

import typing as t

import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import Event, TicketTier

pytestmark = pytest.mark.django_db


def test_list_tiers_exposes_cancellation_policy_fields(
    client: Client,
    public_event: Event,
    tier_factory: t.Callable[..., TicketTier],
) -> None:
    """Buyers must see cancellation/refund policy *before* committing (issue #382)."""
    refund_policy = {
        "tiers": [
            {"hours_before_event": 48, "refund_percentage": "100"},
            {"hours_before_event": 24, "refund_percentage": "50"},
        ],
        "flat_fee": "1.00",
    }
    tier_factory(
        event=public_event,
        name="Cancellable",
        purchasable_by=TicketTier.PurchasableBy.PUBLIC,
        allow_user_cancellation=True,
        cancellation_deadline_hours=24,
        refund_policy=refund_policy,
    )

    response = client.get(reverse("api:tier_list", kwargs={"event_id": public_event.pk}))

    assert response.status_code == 200, response.content
    body = response.json()
    cancellable = next(t for t in body if t["name"] == "Cancellable")
    assert cancellable["allow_user_cancellation"] is True
    assert cancellable["cancellation_deadline_hours"] == 24
    assert cancellable["refund_policy"] == {
        "tiers": [
            {"hours_before_event": 48, "refund_percentage": "100"},
            {"hours_before_event": 24, "refund_percentage": "50"},
        ],
        "flat_fee": "1.00",
    }


def test_list_tiers_defaults_when_cancellation_disabled(
    client: Client,
    public_event: Event,
    tier_factory: t.Callable[..., TicketTier],
) -> None:
    """Tiers without a refund policy serialize defaults — no 500, no missing keys."""
    tier_factory(
        event=public_event,
        name="No-Cancel",
        purchasable_by=TicketTier.PurchasableBy.PUBLIC,
    )

    response = client.get(reverse("api:tier_list", kwargs={"event_id": public_event.pk}))

    assert response.status_code == 200, response.content
    body = response.json()
    no_cancel = next(t for t in body if t["name"] == "No-Cancel")
    assert no_cancel["allow_user_cancellation"] is False
    assert no_cancel["cancellation_deadline_hours"] is None
    assert no_cancel["refund_policy"] is None
