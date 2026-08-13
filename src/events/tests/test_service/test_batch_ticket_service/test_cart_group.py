"""Tests for CartGroup and the dual-form BatchTicketService constructor (#846)."""

from decimal import Decimal

import pytest

from accounts.models import RevelUser
from events.models import Event, TicketTier
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service import BatchTicketService, CartGroup


@pytest.fixture
def second_tier(batch_event: Event) -> TicketTier:
    """A second offline tier on the same event, for multi-group cart tests."""
    return TicketTier.objects.create(
        event=batch_event,
        name="Second Offline Entry",
        price=Decimal("15.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        total_quantity=100,
    )


@pytest.mark.django_db
class TestDualFormConstructor:
    """Both the legacy single-tier form and the new cart form must build a service."""

    def test_single_tier_form_still_works(
        self, batch_event: Event, batch_offline_tier: TicketTier, batch_user: RevelUser
    ) -> None:
        service = BatchTicketService(batch_event, batch_offline_tier, batch_user)
        result = service.create_batch([TicketPurchaseItem(guest_name="Ann")])
        assert isinstance(result, list)
        assert len(result) == 1

    def test_cart_form_stores_groups(
        self, batch_event: Event, batch_offline_tier: TicketTier, batch_user: RevelUser
    ) -> None:
        groups = [CartGroup(tier=batch_offline_tier, items=[TicketPurchaseItem(guest_name="Ann")])]
        service = BatchTicketService(batch_event, user=batch_user, groups=groups)
        assert service.groups == groups

    def test_user_required(self, batch_event: Event, batch_offline_tier: TicketTier) -> None:
        with pytest.raises(TypeError):
            BatchTicketService(batch_event, batch_offline_tier)

    def test_tier_xor_groups(self, batch_event: Event, batch_offline_tier: TicketTier, batch_user: RevelUser) -> None:
        groups = [CartGroup(tier=batch_offline_tier, items=[TicketPurchaseItem()])]
        with pytest.raises(TypeError):
            BatchTicketService(batch_event, batch_offline_tier, batch_user, groups=groups)
        with pytest.raises(TypeError):
            BatchTicketService(batch_event, user=batch_user)

    def test_cart_form_forbids_items_arg(
        self, batch_event: Event, batch_offline_tier: TicketTier, batch_user: RevelUser
    ) -> None:
        groups = [CartGroup(tier=batch_offline_tier, items=[TicketPurchaseItem()])]
        service = BatchTicketService(batch_event, user=batch_user, groups=groups)
        with pytest.raises(TypeError):
            service.create_batch([TicketPurchaseItem()])

    def test_multi_group_not_implemented_yet(
        self,
        batch_event: Event,
        batch_offline_tier: TicketTier,
        second_tier: TicketTier,
        batch_user: RevelUser,
    ) -> None:
        groups = [
            CartGroup(tier=batch_offline_tier, items=[TicketPurchaseItem()]),
            CartGroup(tier=second_tier, items=[TicketPurchaseItem()]),
        ]
        service = BatchTicketService(batch_event, user=batch_user, groups=groups)
        with pytest.raises(NotImplementedError):
            service.create_batch()
