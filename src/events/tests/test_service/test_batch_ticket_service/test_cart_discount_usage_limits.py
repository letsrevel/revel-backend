"""Usage limits are a property of the CART, not of one group (#846 / PR #893).

``validate_cart_discount`` fans a cart code out over the cart's groups and drops the
groups it does not apply to. The drop list is deliberately narrow — scope, free/PWYC
tiers, currency — because a dropped group is *silent*: the buyer gets a 200 with the
code applied to fewer tickets than they asked for, and no explanation.

The per-user usage limit used to be inside that per-group pass, which made it a drop
reason too. A group bigger than the buyer's remaining allowance vanished from the cart
instead of failing it: ``max_uses_per_user=2`` with a 3+1 cart returned 200 with the
code on 1 ticket of 4 — while the very same over-limit purchase on the deprecated
single-tier route, and even a 2+2 split of the same cart (caught by the summed final
re-check), returned 400. These tests pin the limit where it belongs: checked once,
against the summed count of every applicable group.
"""

import typing as t
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events import schema
from events.models import Event, Organization, Ticket, TicketTier
from events.models.discount_code import DiscountCode
from events.service.discount_code_service import validate_cart_discount

pytestmark = pytest.mark.django_db

MAX_USES_MESSAGE = "You have already used this discount code the maximum number of times."
GLOBAL_LIMIT_MESSAGE = "This discount code has reached its usage limit."


def _tier(event: Event, name: str) -> TicketTier:
    """A plain paid offline tier — the code-eligible shape."""
    return TicketTier.objects.create(
        event=event,
        name=name,
        price=Decimal("20.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        price_type=TicketTier.PriceType.FIXED,
        total_quantity=100,
    )


def _group(tier: TicketTier, count: int) -> schema.CheckoutGroupSchema:
    return schema.CheckoutGroupSchema(
        tier_id=tier.id,
        tickets=[schema.TicketPurchaseItem(guest_name=f"Guest {i}") for i in range(count)],
    )


def _cart(
    *groups: schema.CheckoutGroupSchema,
) -> tuple[dict[UUID, TicketTier], list[schema.CheckoutGroupSchema]]:
    """Build the ``(tiers, items)`` pair the caller normally assembles from the payload."""
    tier_ids = [group.tier_id for group in groups]
    tiers = {tier.id: tier for tier in TicketTier.objects.filter(id__in=tier_ids)}
    return tiers, list(groups)


@pytest.fixture
def cart_event(organization: Organization) -> Event:
    """Open public event with no per-user ceiling of its own in the way."""
    return Event.objects.create(
        organization=organization,
        name="Cart Discount Event",
        slug="cart-discount-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        start=timezone.now() + timedelta(days=7),
        status=Event.EventStatus.OPEN,
        max_attendees=100,
        max_tickets_per_user=None,
    )


@pytest.fixture
def tier_a(cart_event: Event) -> TicketTier:
    return _tier(cart_event, "Tier A")


@pytest.fixture
def tier_b(cart_event: Event) -> TicketTier:
    return _tier(cart_event, "Tier B")


def _code(organization: Organization, **kwargs: t.Any) -> DiscountCode:
    return DiscountCode.objects.create(
        code="CART10",
        organization=organization,
        discount_type=DiscountCode.DiscountType.PERCENTAGE,
        discount_value=Decimal("10.00"),
        currency="EUR",
        is_active=True,
        **kwargs,
    )


class TestPerUserLimitIsCartWide:
    """The per-user allowance is measured against the cart, never against one group."""

    @pytest.mark.parametrize(
        "split",
        [
            pytest.param((3, 1), id="over-limit-group-plus-small-group"),
            pytest.param((2, 2), id="both-groups-within-limit-individually"),
            pytest.param((1, 3), id="over-limit-group-second"),
        ],
    )
    def test_a_cart_of_four_over_a_limit_of_two_is_rejected(
        self,
        cart_event: Event,
        tier_a: TicketTier,
        tier_b: TicketTier,
        batch_user: RevelUser,
        split: tuple[int, int],
    ) -> None:
        """Four tickets against ``max_uses_per_user=2`` fail the cart however they are split.

        The 3+1 split is the regression: the oversized group used to be dropped
        silently, leaving a 1-ticket cart that passed the summed re-check.
        """
        _code(cart_event.organization, max_uses_per_user=2)
        tiers, items = _cart(_group(tier_a, split[0]), _group(tier_b, split[1]))

        with pytest.raises(HttpError) as exc_info:
            validate_cart_discount("CART10", cart_event, tiers, items, batch_user)

        assert exc_info.value.status_code == 400
        assert str(exc_info.value.message) == MAX_USES_MESSAGE

    def test_a_cart_within_the_limit_keeps_every_group(
        self, cart_event: Event, tier_a: TicketTier, tier_b: TicketTier, batch_user: RevelUser
    ) -> None:
        """Same 3+1 cart, allowance of 4: nothing is dropped, the code covers all four."""
        code = _code(cart_event.organization, max_uses_per_user=4)
        tiers, items = _cart(_group(tier_a, 3), _group(tier_b, 1))

        dc, valid_tier_ids = validate_cart_discount("CART10", cart_event, tiers, items, batch_user)

        assert dc.id == code.id
        assert valid_tier_ids == {tier_a.id, tier_b.id}

    def test_prior_tickets_count_toward_the_cart_allowance(
        self, cart_event: Event, tier_a: TicketTier, tier_b: TicketTier, batch_user: RevelUser
    ) -> None:
        """An allowance of 4 already half-spent leaves room for 2, not for this 3+1 cart."""
        code = _code(cart_event.organization, max_uses_per_user=4)
        for _ in range(2):
            Ticket.objects.create(
                event=cart_event,
                tier=tier_a,
                user=batch_user,
                status=Ticket.TicketStatus.ACTIVE,
                discount_code=code,
            )
        tiers, items = _cart(_group(tier_a, 3), _group(tier_b, 1))

        with pytest.raises(HttpError) as exc_info:
            validate_cart_discount("CART10", cart_event, tiers, items, batch_user)

        assert exc_info.value.status_code == 400
        assert str(exc_info.value.message) == MAX_USES_MESSAGE


class TestScopeNarrowingStillDropsSilently:
    """Genuine applicability drops are unchanged — only usage limits moved."""

    def test_out_of_scope_group_is_dropped_not_rejected(
        self, cart_event: Event, tier_a: TicketTier, tier_b: TicketTier, batch_user: RevelUser
    ) -> None:
        """A code scoped to tier A leaves tier B alone instead of failing the cart."""
        code = _code(cart_event.organization, max_uses_per_user=10)
        code.tiers.add(tier_a)
        tiers, items = _cart(_group(tier_a, 2), _group(tier_b, 2))

        dc, valid_tier_ids = validate_cart_discount("CART10", cart_event, tiers, items, batch_user)

        assert dc.id == code.id
        assert valid_tier_ids == {tier_a.id}

    def test_the_limit_is_summed_over_the_surviving_groups_only(
        self, cart_event: Event, tier_a: TicketTier, tier_b: TicketTier, batch_user: RevelUser
    ) -> None:
        """Scoped to A: the 3 A-tickets are what the allowance of 3 must cover, not all 4."""
        code = _code(cart_event.organization, max_uses_per_user=3)
        code.tiers.add(tier_a)
        tiers, items = _cart(_group(tier_a, 3), _group(tier_b, 1))

        dc, valid_tier_ids = validate_cart_discount("CART10", cart_event, tiers, items, batch_user)

        assert dc.id == code.id
        assert valid_tier_ids == {tier_a.id}

    def test_a_scoped_cart_still_fails_when_the_covered_groups_exceed_the_limit(
        self, cart_event: Event, tier_a: TicketTier, tier_b: TicketTier, batch_user: RevelUser
    ) -> None:
        """Same cart, allowance of 2: the surviving 3 tickets are one too many."""
        code = _code(cart_event.organization, max_uses_per_user=2)
        code.tiers.add(tier_a)
        tiers, items = _cart(_group(tier_a, 3), _group(tier_b, 1))

        with pytest.raises(HttpError) as exc_info:
            validate_cart_discount("CART10", cart_event, tiers, items, batch_user)

        assert exc_info.value.status_code == 400
        assert str(exc_info.value.message) == MAX_USES_MESSAGE


class TestGlobalLimit:
    """The global counter is code-level, so it fails every group identically."""

    def test_exhausted_global_limit_rejects_the_cart(
        self, cart_event: Event, tier_a: TicketTier, tier_b: TicketTier, batch_user: RevelUser
    ) -> None:
        """No group can qualify, and the shared reason is surfaced verbatim."""
        _code(cart_event.organization, max_uses_per_user=10, max_uses=5, times_used=5)
        tiers, items = _cart(_group(tier_a, 3), _group(tier_b, 1))

        with pytest.raises(HttpError) as exc_info:
            validate_cart_discount("CART10", cart_event, tiers, items, batch_user)

        assert exc_info.value.status_code == 400
        assert str(exc_info.value.message) == GLOBAL_LIMIT_MESSAGE
