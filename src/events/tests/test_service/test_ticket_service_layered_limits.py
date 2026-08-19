"""Tests for layered per-user limits (#846 Decision 4) in the listing surface.

`get_user_event_status` must thread the precomputed event-wide ticket count
into `get_remaining_tickets` (via `user_event_count`) so:
- the event-level cap is shared across every tier (not just the current one), and
- the listing does not issue one extra "count tickets across the event" query
  per eligible tier.
"""

import typing as t
from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Event, Organization, Ticket, TicketTier
from events.service.event_manager import EventUserEligibility
from events.service.ticket_service import UserEventStatus, get_user_event_status

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner(revel_user_factory: RevelUserFactory) -> RevelUser:
    """Organization owner."""
    return revel_user_factory(username="layered_owner")


@pytest.fixture
def org(owner: RevelUser) -> Organization:
    """Test organization."""
    return Organization.objects.create(name="Layered Org", slug="layered-org", owner=owner)


@pytest.fixture
def event(org: Organization) -> Event:
    """Public event requiring tickets, with an event-wide per-user cap of 3."""
    ev = Event.objects.create(
        organization=org,
        name="Layered Event",
        slug="layered-event",
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        status=Event.EventStatus.OPEN,
        start=timezone.now() + timedelta(days=7),
        end=timezone.now() + timedelta(days=8),
        requires_ticket=True,
        max_tickets_per_user=3,
    )
    ev.ticket_tiers.all().delete()
    return ev


@pytest.fixture
def tier_a(event: Event) -> TicketTier:
    """Tier A: no per-tier cap."""
    return TicketTier.objects.create(
        event=event,
        name="Tier A",
        payment_method=TicketTier.PaymentMethod.FREE,
    )


@pytest.fixture
def tier_b(event: Event) -> TicketTier:
    """Tier B: per-tier cap of 1."""
    return TicketTier.objects.create(
        event=event,
        name="Tier B",
        payment_method=TicketTier.PaymentMethod.FREE,
        max_tickets_per_user=1,
    )


@pytest.fixture
def user(revel_user_factory: RevelUserFactory) -> RevelUser:
    """Regular user."""
    return revel_user_factory(username="layered_user")


class TestListingLayeredLimits:
    """Pin the listing's remaining-ticket math against the layered-limits contract."""

    def test_event_cap_is_shared_across_tiers(
        self,
        event: Event,
        tier_a: TicketTier,
        tier_b: TicketTier,
        user: RevelUser,
    ) -> None:
        """Event cap 3, one ticket already in tier A: both tiers see the shared budget.

        Tier A has no per-tier cap, so its remaining is bound only by the
        event-wide cap: 3 - 1 = 2.
        Tier B has a per-tier cap of 1, so its remaining is
        min(1 - 0, 3 - 1) = min(1, 2) = 1.
        """
        Ticket.objects.create(
            event=event,
            tier=tier_a,
            user=user,
            guest_name=user.username,
            status=Ticket.TicketStatus.ACTIVE,
        )

        status = get_user_event_status(event, user)

        assert isinstance(status, UserEventStatus)
        remaining = {r.tier_id: r for r in status.remaining_tickets}
        assert remaining[tier_a.id].remaining == 2
        assert remaining[tier_b.id].remaining == 1

    def test_event_remaining_exposes_the_shared_budget(
        self,
        event: Event,
        tier_a: TicketTier,
        tier_b: TicketTier,
        user: RevelUser,
    ) -> None:
        """The event-scoped term is exposed on its own so the FE can do cross-tier math (#901).

        Event cap 3, one ticket already held: the shared budget is 2, regardless
        of which tier the FE is looking at.
        """
        Ticket.objects.create(
            event=event,
            tier=tier_a,
            user=user,
            guest_name=user.username,
            status=Ticket.TicketStatus.ACTIVE,
        )

        status = get_user_event_status(event, user)

        assert isinstance(status, UserEventStatus)
        assert status.event_remaining == 2

    def test_event_remaining_is_none_without_an_event_cap(
        self,
        event: Event,
        tier_a: TicketTier,
        user: RevelUser,
    ) -> None:
        """No event-level cap means no shared budget to report."""
        event.max_tickets_per_user = None
        event.save(update_fields=["max_tickets_per_user"])
        Ticket.objects.create(
            event=event,
            tier=tier_a,
            user=user,
            guest_name=user.username,
            status=Ticket.TicketStatus.ACTIVE,
        )

        status = get_user_event_status(event, user)

        assert isinstance(status, UserEventStatus)
        assert status.event_remaining is None

    def test_event_remaining_floors_at_zero(
        self,
        event: Event,
        tier_a: TicketTier,
        user: RevelUser,
    ) -> None:
        """A cap lowered below what the user already holds reports 0, never a negative budget."""
        for i in range(4):
            Ticket.objects.create(
                event=event,
                tier=tier_a,
                user=user,
                guest_name=f"{user.username}-{i}",
                status=Ticket.TicketStatus.ACTIVE,
            )

        status = get_user_event_status(event, user)

        assert isinstance(status, UserEventStatus)
        assert status.event_remaining == 0

    def test_event_remaining_counts_pending_tickets(
        self,
        event: Event,
        tier_a: TicketTier,
        tier_b: TicketTier,
        user: RevelUser,
    ) -> None:
        """PENDING tickets eat into the budget the same way ACTIVE ones do."""
        Ticket.objects.create(
            event=event,
            tier=tier_a,
            user=user,
            guest_name=user.username,
            status=Ticket.TicketStatus.PENDING,
        )
        Ticket.objects.create(
            event=event,
            tier=tier_b,
            user=user,
            guest_name=user.username,
            status=Ticket.TicketStatus.ACTIVE,
        )

        status = get_user_event_status(event, user)

        assert isinstance(status, UserEventStatus)
        assert status.event_remaining == 1

    def test_eligibility_shape_carries_the_full_budget(
        self,
        event: Event,
        tier_a: TicketTier,
        user: RevelUser,
    ) -> None:
        """A user holding nothing gets the eligibility shape, which still reports the budget (#901).

        `get_user_event_status` returns `EventUserEligibility` for anyone with no tickets,
        and that shape carried no purchase limits at all — so the first stepper interaction
        of a cart had nothing to cap against.
        """
        status = get_user_event_status(event, user)

        assert isinstance(status, EventUserEligibility)
        assert status.event_remaining == 3  # nothing held yet, so the whole cap is available

    def test_listing_does_not_issue_a_query_per_tier_for_event_count(
        self,
        event: Event,
        tier_a: TicketTier,
        tier_b: TicketTier,
        user: RevelUser,
        django_assert_max_num_queries: t.Callable[[int], t.ContextManager[None]],
    ) -> None:
        """The event-wide ticket count must be precomputed once, not re-queried per tier.

        Without threading `user_event_count` through, `get_remaining_tickets`
        falls back to querying the database for every eligible tier that sets
        `event.max_tickets_per_user` — an N+1 that grows with tier count.

        The bound came down from 13 to 12 in #901: the per-tier counts are now taken from
        the ticket list the function already loads, so the grouped aggregate that used to
        re-read the same rows is gone.
        """
        Ticket.objects.create(
            event=event,
            tier=tier_a,
            user=user,
            guest_name=user.username,
            status=Ticket.TicketStatus.ACTIVE,
        )

        with django_assert_max_num_queries(12):
            get_user_event_status(event, user)
