"""Revel-side pause: a paused tier is not purchasable and is reported as paused, not ended."""

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Event, TicketTier
from events.service import ticket_service
from events.service.event_manager.enums import ReasonCode
from events.service.event_manager.gates import TicketSalesGate

pytestmark = pytest.mark.django_db


@pytest.fixture
def ticket_tier(event: Event) -> TicketTier:
    """`event` comes from src/events/tests/conftest.py (owner = organization_owner_user).

    Deletes the default tier auto-created by the `handle_event_save` signal
    (events/signals.py) so the returned tier is the event's only ticket tier —
    needed for the "all tiers paused" gate assertion below.
    """
    event.ticket_tiers.all().delete()
    return TicketTier.objects.create(event=event, name="General", price=10, total_quantity=100)


@pytest.fixture
def organization_owner_client(organization_owner_user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(organization_owner_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


def test_can_purchase_false_when_paused(ticket_tier: TicketTier) -> None:
    assert ticket_tier.can_purchase() is True
    ticket_tier.sales_paused = True
    assert ticket_tier.can_purchase() is False


def test_eligible_tiers_skip_paused(ticket_tier: TicketTier) -> None:
    ticket_tier.sales_paused = True
    ticket_tier.save(update_fields=["sales_paused"])
    tiers = ticket_service.get_eligible_tiers(ticket_tier.event, ticket_tier.event.organization.owner)
    assert ticket_tier not in tiers


def test_sales_gate_reports_paused_when_all_tiers_paused(ticket_tier: TicketTier) -> None:
    ticket_tier.sales_paused = True
    ticket_tier.save(update_fields=["sales_paused"])
    from events.service.event_manager.service import EligibilityService

    handler = EligibilityService(ticket_tier.event.organization.owner, ticket_tier.event)
    result = TicketSalesGate(handler).check()
    assert result is not None
    assert result.reason_code == ReasonCode.SALES_PAUSED


def test_admin_can_toggle_sales_paused(organization_owner_client: Client, ticket_tier: TicketTier) -> None:
    url = reverse("api:update_ticket_tier", kwargs={"event_id": ticket_tier.event_id, "tier_id": ticket_tier.id})
    response = organization_owner_client.put(
        url, data=orjson.dumps({"sales_paused": True}), content_type="application/json"
    )
    assert response.status_code == 200, response.content
    assert response.json()["sales_paused"] is True
    ticket_tier.refresh_from_db()
    assert ticket_tier.sales_paused is True


def test_paused_tier_403s_with_a_pause_message_not_a_sale_window_one() -> None:
    """A paused tier's sale window is usually wide open; saying otherwise misleads the buyer."""
    from ninja.errors import HttpError

    from events.service.batch_ticket_service.eligibility import assert_sale_window

    tier = TicketTier(name="General", price=10, sales_paused=True)
    with pytest.raises(HttpError) as exc:
        assert_sale_window(tier)
    assert "paused" in str(exc.value).lower()
    assert "sale window" not in str(exc.value).lower()


def test_anonymous_listing_does_not_advertise_a_paused_tier(event: Event, ticket_tier: TicketTier) -> None:
    """The public tier list must not report ``can_purchase`` for a tier nobody can buy."""
    assert ticket_service.anonymous_can_purchase(ticket_tier, event, None) is True
    ticket_tier.sales_paused = True
    assert ticket_service.anonymous_can_purchase(ticket_tier, event, None) is False
