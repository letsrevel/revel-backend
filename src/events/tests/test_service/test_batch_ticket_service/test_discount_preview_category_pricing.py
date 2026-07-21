"""The discount-code preview must never quote a price the charge will not honour.

``preview_discount_code`` used to price off ``tier.price`` unconditionally, which was
correct while every tier was flat. Once ``category_prices`` became the sole seat-pricing
mechanism, ``tier.price`` turned into a decoy on a mapped tier — nothing validates it any
more — and this endpoint became a second price authority, quoting off the wrong number.

The concrete failure these tests pin: a tier mapped Premium 80.00 / Standard 30.00 with a
leftover ``tier.price = 5.00``. A 10% code was quoted as **4.50** while checkout charged
**72.00** for the Premium seat the buyer had picked.

The fix adds no second price computation: on a mapped tier the endpoint simply declines to
quote a single number (``discounted_price`` is already ``Decimal | None``), because there
is no such number — each seat discounts off its own category price. The buyer's real total
comes from the seat-aware VAT preview, which reaches
``events.service.seating.pricing.resolve_seat_price``, the one price authority. What this
endpoint exists to answer — is the code valid, and what kind and size of discount is it —
still comes back in full.
"""

from decimal import Decimal

import pytest

from accounts.models import RevelUser
from events.models import DiscountCode, Event, Organization, PriceCategory, TicketTier, VenueSeat, VenueSector
from events.service import discount_code_service
from events.service.seating.pricing import build_batch_pricing
from events.tests.test_service.test_batch_ticket_service.conftest import FLAT, make_category_tier

pytestmark = pytest.mark.django_db

DECOY_FLAT = Decimal("5.00")


@pytest.fixture
def pct10(seated_org: Organization) -> DiscountCode:
    """10% off — 80.00 → 72.00, and the decoy 5.00 → 4.50."""
    return DiscountCode.objects.create(
        code="PCT10",
        organization=seated_org,
        discount_type=DiscountCode.DiscountType.PERCENTAGE,
        discount_value=Decimal("10.00"),
        currency="EUR",
        max_uses_per_user=10,
    )


def test_mapped_tier_declines_to_quote_a_single_discounted_price(
    seated_event: Event,
    seated_org: Organization,
    sector: VenueSector,
    categories: tuple[PriceCategory, PriceCategory],
    seats: list[VenueSeat],
    pct10: DiscountCode,
    member_user: RevelUser,
) -> None:
    """No number is better than 4.50 when the charge is 72.00.

    Pre-fix this asserted ``discounted_price == Decimal("4.50")`` — 10% off a flat price
    the tier does not actually sell at — against a charge of 72.00 for the same code on
    the same tier. The quote and the charge now agree by construction: the endpoint
    quotes nothing, and the only number in play comes from the single price authority.
    """
    tier = make_category_tier(seated_event, sector, categories, TicketTier.PaymentMethod.ONLINE, flat=DECOY_FLAT)

    result = discount_code_service.preview_discount_code("PCT10", seated_org, tier, member_user)

    assert result.valid is True
    assert result.discounted_price is None, "a mapped tier has no single discounted price to quote"
    # The reason the endpoint exists still answered in full.
    assert result.discount_type == DiscountCode.DiscountType.PERCENTAGE
    assert result.discount_value == Decimal("10.00")

    # And what the buyer would actually be charged for the Premium seat they picked.
    charged = build_batch_pricing(tier, [seats[0]], discount_code=pct10).total
    assert charged == Decimal("72.00")
    assert charged != DECOY_FLAT * Decimal("0.90"), "the decoy flat price is not what anyone pays"


def test_flat_tier_still_quotes_the_discounted_price(
    seated_event: Event,
    seated_org: Organization,
    pct10: DiscountCode,
    member_user: RevelUser,
) -> None:
    """The unmapped, pre-seating world must not move: 50.00 at 10% is still quoted as 45.00.

    Pinned so "decline to quote" is not mistaken for a licence to stop quoting everywhere —
    on a flat tier ``tier.price`` *is* what the buyer pays, and the quote equals the charge.
    """
    tier = TicketTier.objects.create(
        event=seated_event,
        name="GA",
        price=FLAT,
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
        total_quantity=50,
        max_tickets_per_user=5,
    )

    result = discount_code_service.preview_discount_code("PCT10", seated_org, tier, member_user)

    assert result.valid is True
    assert result.discounted_price == Decimal("45.00")
    assert result.discounted_price == build_batch_pricing(tier, [None], discount_code=pct10).total
