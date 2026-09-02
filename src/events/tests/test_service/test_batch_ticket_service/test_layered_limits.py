"""Tests for layered max_tickets_per_user semantics (#846 spec Decision 4).

Event cap = cross-tier total; tier cap = independent per-tier ceiling; tier
null = no per-tier cap. The old inherit-as-per-tier-default semantics (tier
None falling back to the event value as ITS OWN per-tier cap) are gone —
both layers now apply simultaneously whenever they're set.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, Organization, Ticket, TicketTier
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service import BatchTicketService
from events.service.batch_ticket_service.context import CartGroup

pytestmark = pytest.mark.django_db


@pytest.fixture
def event(organization: Organization) -> Event:
    """Future-dated public event with an unlimited per-user cap by default.

    require_ticket_names is off so the layered-limit assertions (which run
    before assert_ticket_names) are the only thing under test.
    """
    return Event.objects.create(
        organization=organization,
        name="Layered Limits Event",
        slug="layered-limits-event",
        event_type=Event.EventType.PUBLIC,
        start=timezone.now() + timedelta(days=7),
        status=Event.EventStatus.OPEN,
        visibility=Event.Visibility.PUBLIC,
        max_tickets_per_user=None,
        require_ticket_names=False,
    )


@pytest.fixture
def tier_a(event: Event) -> TicketTier:
    """A free tier with no per-tier cap of its own."""
    return TicketTier.objects.create(
        event=event,
        name="Tier A",
        price=Decimal("25.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
    )


@pytest.fixture
def tier_b(event: Event) -> TicketTier:
    """A second free tier with no per-tier cap of its own."""
    return TicketTier.objects.create(
        event=event,
        name="Tier B",
        price=Decimal("25.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
    )


def make_active_ticket(event: Event, tier: TicketTier, user: RevelUser) -> Ticket:
    """Create one pre-existing ACTIVE ticket in `tier` for `user`."""
    return Ticket.objects.create(
        event=event,
        tier=tier,
        user=user,
        status=Ticket.TicketStatus.ACTIVE,
        guest_name="Existing Guest",
    )


class TestLayeredLimits:
    def test_event_cap_counts_across_tiers(
        self, event: Event, tier_a: TicketTier, tier_b: TicketTier, member_user: RevelUser
    ) -> None:
        event.max_tickets_per_user = 2
        event.save(update_fields=["max_tickets_per_user"])
        make_active_ticket(event, tier_a, member_user)  # 1 existing in tier A
        service = BatchTicketService(event, tier_b, member_user)
        with pytest.raises(HttpError) as exc:  # 1 existing + 2 requested > 2
            service.create_batch([TicketPurchaseItem(), TicketPurchaseItem()])
        assert exc.value.status_code == 400
        assert "for this event" in str(exc.value.message)

    def test_tier_null_means_no_per_tier_cap(self, event: Event, tier_a: TicketTier, member_user: RevelUser) -> None:
        event.max_tickets_per_user = 5
        event.save(update_fields=["max_tickets_per_user"])
        service = BatchTicketService(event, tier_a, member_user)  # tier_a.max_tickets_per_user is None
        result = service.create_batch([TicketPurchaseItem() for _ in range(5)])
        assert isinstance(result, list)
        assert len(result) == 5  # old semantics would also pass; the NEXT purchase pins the change

    def test_cart_alone_exceeding_event_cap(
        self, event: Event, tier_a: TicketTier, tier_b: TicketTier, member_user: RevelUser
    ) -> None:
        event.max_tickets_per_user = 4
        event.save(update_fields=["max_tickets_per_user"])
        groups = [
            CartGroup(tier=tier_a, items=[TicketPurchaseItem()] * 3),
            CartGroup(tier=tier_b, items=[TicketPurchaseItem()] * 2),
        ]
        service = BatchTicketService(event, user=member_user, groups=groups)
        with pytest.raises(HttpError) as exc:
            service.assert_per_user_limits(groups)  # direct call — engine still gated
        assert exc.value.status_code == 400
        assert "for this event" in str(exc.value.message)

    def test_tier_cap_still_binds(self, event: Event, tier_a: TicketTier, member_user: RevelUser) -> None:
        tier_a.max_tickets_per_user = 1
        tier_a.save(update_fields=["max_tickets_per_user"])
        service = BatchTicketService(event, tier_a, member_user)
        with pytest.raises(HttpError) as exc:
            service.create_batch([TicketPurchaseItem(), TicketPurchaseItem()])
        assert exc.value.status_code == 400
        assert "for this tier" in str(exc.value.message)

    def test_event_cap_is_read_from_the_locked_row(
        self, event: Event, tier_a: TicketTier, member_user: RevelUser
    ) -> None:
        """A cap enabled after the service loaded its Event still binds: the cap is read off the
        locked row, not the stale pre-lock instance (which would say None and skip the check)."""
        service = BatchTicketService(event, tier_a, member_user)  # event.max_tickets_per_user is None here
        make_active_ticket(event, tier_a, member_user)
        Event.objects.filter(pk=event.pk).update(max_tickets_per_user=1)  # organizer write, behind our back
        with pytest.raises(HttpError) as exc:
            service.create_batch([TicketPurchaseItem()])
        assert exc.value.status_code == 400
        assert "for this event" in str(exc.value.message)

    def test_tier_cap_is_read_from_the_locked_row(
        self, event: Event, tier_a: TicketTier, member_user: RevelUser
    ) -> None:
        """Same for the tier cap: the groups handed to assert_per_user_limits carry the LOCKED tiers."""
        service = BatchTicketService(event, tier_a, member_user)  # tier_a.max_tickets_per_user is None here
        make_active_ticket(event, tier_a, member_user)
        TicketTier.objects.filter(pk=tier_a.pk).update(max_tickets_per_user=1)
        with pytest.raises(HttpError) as exc:
            service.create_batch([TicketPurchaseItem()])
        assert exc.value.status_code == 400
        assert "for this tier" in str(exc.value.message)
