"""Seat-aware VAT preview for category-priced tiers (plan Task 13, spec §7).

The preview is the last number a buyer sees before Stripe, and for a business buyer
it is a legally-shaped number they will reconcile against an invoice. On a
category-priced tier the old preview quoted ``tier.price`` for every ticket, so the
buyers paying premium prices — precisely the ones who care — were shown VAT computed
on a total they were never charged.

Cart shape throughout, reusing the module conftest: seat A1 Premium 80.00,
A2 Standard 30.00, A3 unpainted → the tier's flat 50.00. Gross total 160.00.

Best-available tiers preview the same prices without any seats at all: the buyer names a
*zone* (``price_category_id``) instead, since the picker only assigns seats at hold time.
Both shapes must produce the identical response — see ``TestModeAgnosticResponseShape``.

The load-bearing test in this module is
``TestPreviewMatchesTheCharge::test_preview_total_equals_the_sum_of_the_payment_rows``:
a preview that disagrees with the charge is the bug this task exists to close.
"""

import dataclasses
import typing as t
from decimal import Decimal

import orjson
import pytest
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    DiscountCode,
    Event,
    Organization,
    Payment,
    PriceCategory,
    Ticket,
    TicketTier,
    VenueSeat,
    VenueSector,
)
from events.schema import BuyerBillingInfoSchema, TicketPurchaseItem, VATPreviewItemSchema
from events.service.attendee_vat_service import VATPreviewLineItem, calculate_vat_preview
from events.service.batch_ticket_service import BatchTicketService
from events.tests.test_service.test_batch_ticket_service.conftest import (
    FLAT,
    PREMIUM,
    STANDARD,
    make_category_tier,
)

pytestmark = pytest.mark.django_db

GROSS_TOTAL = PREMIUM + STANDARD + FLAT  # 160.00
VAT_RATE = Decimal("22.00")
ZERO = Decimal("0.00")


@pytest.fixture
def vat_org(seated_org: Organization) -> Organization:
    """The seated org, VAT-registered in Italy at 22%."""
    seated_org.vat_country_code = "IT"
    seated_org.vat_rate = VAT_RATE
    seated_org.save()
    return seated_org


@pytest.fixture
def online_tier(
    seated_event: Event,
    vat_org: Organization,
    sector: VenueSector,
    categories: tuple[PriceCategory, PriceCategory],
    seats: list[VenueSeat],
) -> TicketTier:
    """Category-priced ONLINE tier: Premium 80, Standard 30, unpainted 50."""
    return make_category_tier(seated_event, sector, categories, TicketTier.PaymentMethod.ONLINE)


@pytest.fixture
def flat_tier(seated_event: Event, vat_org: Organization) -> TicketTier:
    """A plain general-admission tier — the pre-seating world, which must not move."""
    return TicketTier.objects.create(
        event=seated_event,
        name="GA Preview",
        price=FLAT,
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
        total_quantity=50,
        max_tickets_per_user=5,
    )


@pytest.fixture
def ba_tier(
    seated_event: Event,
    vat_org: Organization,
    sector: VenueSector,
    categories: tuple[PriceCategory, PriceCategory],
    seats: list[VenueSeat],
) -> TicketTier:
    """Best-available twin of ``online_tier``: same zones, same prices, no seat picking.

    Its ``category_prices`` keys *are* its sellable zones (spec §4.2) — the buyer names
    one per request and the picker assigns seats inside it later.
    """
    premium, standard = categories
    return TicketTier.objects.create(
        event=seated_event,
        name="Stalls BA",
        price=FLAT,
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
        total_quantity=50,
        max_tickets_per_user=5,
        seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
        venue=sector.venue,
        sector=sector,
        category_prices={str(premium.pk): str(PREMIUM), str(standard.pk): str(STANDARD)},
    )


@pytest.fixture
def ba_unmapped_tier(seated_event: Event, vat_org: Organization, sector: VenueSector) -> TicketTier:
    """A best-available tier with no category map: one flat price for the whole sector."""
    return TicketTier.objects.create(
        event=seated_event,
        name="Stalls BA Flat",
        price=FLAT,
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
        total_quantity=50,
        max_tickets_per_user=5,
        seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
        venue=sector.venue,
        sector=sector,
    )


@pytest.fixture
def unpriced_zone(sector: VenueSector) -> PriceCategory:
    """A category of the venue that no tier in these tests prices."""
    return PriceCategory.objects.create(venue=sector.venue, name="Box", color="#0000aa")


@pytest.fixture
def pct10(vat_org: Organization) -> DiscountCode:
    """10% off — 80 → 72, 30 → 27, 50 → 45."""
    return DiscountCode.objects.create(
        code="PCT10",
        organization=vat_org,
        discount_type=DiscountCode.DiscountType.PERCENTAGE,
        discount_value=Decimal("10.00"),
        currency="EUR",
        max_uses_per_user=10,
    )


def _domestic() -> BuyerBillingInfoSchema:
    """An Italian consumer buying from an Italian seller: full 22% VAT, gross = list."""
    return BuyerBillingInfoSchema(billing_name="Mario Rossi", vat_country_code="IT")  # type: ignore[call-arg]


def _non_eu() -> BuyerBillingInfoSchema:
    """A US buyer: export of services, zero-rated, so gross drops to the net amount."""
    return BuyerBillingInfoSchema(billing_name="John Doe", vat_country_code="US")  # type: ignore[call-arg]


def _preview(
    event: Event,
    tier: TicketTier,
    *,
    seats: list[VenueSeat] | None = None,
    count: int | None = None,
    zone: PriceCategory | None = None,
    billing: BuyerBillingInfoSchema | None = None,
    discount_code: str | None = None,
    price_per_ticket: Decimal | None = None,
) -> t.Any:
    seat_ids = [seat.pk for seat in seats] if seats else []
    item = VATPreviewItemSchema(
        tier_id=tier.pk,
        count=count if count is not None else (len(seat_ids) or 1),
        seat_ids=seat_ids,
        price_category_id=zone.pk if zone is not None else None,
    )
    return calculate_vat_preview(
        event,
        billing or _domestic(),
        [item],
        discount_code=discount_code,
        price_per_ticket=price_per_ticket,
    )


def _quote(line_items: list[VATPreviewLineItem]) -> list[tuple[str | None, int, Decimal]]:
    """What the buyer reads off the preview: (category, how many, at what price)."""
    return [(li.price_category_name, li.ticket_count, li.unit_price_gross) for li in line_items]


class TestBackwardCompatibility:
    """A caller that sends no seats must get exactly today's answer."""

    def test_flat_tier_without_seats_is_one_line_at_the_list_price(
        self, seated_event: Event, flat_tier: TicketTier
    ) -> None:
        """Three GA tickets: one line, 3 × 50.00, 22% VAT on 150.00."""
        result = _preview(seated_event, flat_tier, count=3)

        assert _quote(result.line_items) == [(None, 3, FLAT)]
        assert result.total_gross == Decimal("150.00")
        # 50.00 gross at 22% inclusive → 40.98 net + 9.02 VAT, times three.
        assert result.total_net == Decimal("122.94")
        assert result.total_vat == Decimal("27.06")
        assert result.reverse_charge is False

    def test_category_priced_tier_without_seats_is_refused(self, seated_event: Event, online_tier: TicketTier) -> None:
        """Backward compatibility does not extend to tiers that could not previously exist.

        Quoting ``tier.price`` here would promise a total checkout will not honour — the
        exact disagreement this endpoint exists to prevent, and silent for precisely the
        buyers paying premium prices. ``category_prices`` ships with this feature, so no
        pre-existing client can be previewing such a tier: refusing breaks nobody, and it
        stops a client shipping the wrong call.
        """
        with pytest.raises(HttpError) as exc_info:
            _preview(seated_event, online_tier, count=3)

        assert exc_info.value.status_code == 400
        assert "seat_ids are required" in str(exc_info.value)

    def test_pwyc_override_still_wins_over_everything(
        self, seated_event: Event, online_tier: TicketTier, seats: list[VenueSeat], pct10: DiscountCode
    ) -> None:
        """PWYC prices the whole cart uniformly and ignores the category map — as at checkout."""
        result = _preview(seated_event, online_tier, seats=seats, discount_code="PCT10", price_per_ticket=Decimal("10"))

        assert _quote(result.line_items) == [
            ("Premium", 1, Decimal("10")),
            ("Standard", 1, Decimal("10")),
            (None, 1, Decimal("10")),
        ]
        assert result.total_gross == Decimal("30.00")


class TestMixedCategoryCart:
    """One line per distinct unit price, named by its price category."""

    def test_mixed_cart_is_split_into_named_lines(
        self, seated_event: Event, online_tier: TicketTier, seats: list[VenueSeat]
    ) -> None:
        """80 Premium / 30 Standard / 50 unpainted — three lines, not one anonymous 150."""
        result = _preview(seated_event, online_tier, seats=seats)

        assert _quote(result.line_items) == [
            ("Premium", 1, PREMIUM),
            ("Standard", 1, STANDARD),
            (None, 1, FLAT),
        ]
        assert result.total_gross == GROSS_TOTAL
        assert all(li.tier_name == online_tier.name for li in result.line_items)

    def test_seats_in_the_same_category_collapse_into_one_line(
        self, seated_event: Event, online_tier: TicketTier, seats: list[VenueSeat], sector: VenueSector
    ) -> None:
        """ "2 × Premium @ 80.00" — an invoice line, not two anonymous ones."""
        premium_twin = VenueSeat.objects.create(
            sector=sector,
            label="A4",
            row_label="A",
            number=4,
            position={"x": 3, "y": 0},
            is_active=True,
            default_price_category=seats[0].default_price_category,
        )
        result = _preview(seated_event, online_tier, seats=[seats[0], premium_twin, seats[1]])

        assert _quote(result.line_items) == [("Premium", 2, PREMIUM), ("Standard", 1, STANDARD)]
        assert result.line_items[0].line_gross == Decimal("160.00")
        assert result.total_gross == Decimal("190.00")

    def test_line_totals_sum_to_the_reported_totals(
        self, seated_event: Event, online_tier: TicketTier, seats: list[VenueSeat]
    ) -> None:
        """The accounting identity has to hold across the split, not just per line."""
        result = _preview(seated_event, online_tier, seats=seats)

        assert sum(li.line_net for li in result.line_items) == result.total_net
        assert sum(li.line_vat for li in result.line_items) == result.total_vat
        assert sum(li.line_gross for li in result.line_items) == result.total_gross
        assert result.total_net + result.total_vat == result.total_gross

    def test_non_eu_buyer_is_zero_rated_on_every_line(
        self, seated_event: Event, online_tier: TicketTier, seats: list[VenueSeat]
    ) -> None:
        """Export of services: each seat's own list price drops to its own net.

        ``unit_price_gross`` keeps its pre-existing meaning — the *list* price the seat
        carries, before the buyer's VAT treatment — while ``line_*`` carry what is
        actually charged. That split predates this change and is preserved verbatim;
        what is new is that both are now per category instead of per tier.
        """
        result = _preview(seated_event, online_tier, seats=seats, billing=_non_eu())

        assert _quote(result.line_items) == [
            ("Premium", 1, PREMIUM),
            ("Standard", 1, STANDARD),
            (None, 1, FLAT),
        ]
        # 80/30/50 inclusive of 22% → 65.57 / 24.59 / 40.98
        assert [li.line_gross for li in result.line_items] == [
            Decimal("65.57"),
            Decimal("24.59"),
            Decimal("40.98"),
        ]
        assert result.total_vat == Decimal("0.00")
        assert result.total_gross == Decimal("131.14")

    def test_partial_seat_context_is_refused(self, online_tier: TicketTier, seats: list[VenueSeat]) -> None:
        """Two seats for three tickets would silently price the third at the flat rate."""
        with pytest.raises(ValueError, match="exactly `count` entries"):
            VATPreviewItemSchema(tier_id=online_tier.pk, count=3, seat_ids=[seats[0].pk, seats[1].pk])

    def test_unknown_seat_is_refused(
        self, seated_event: Event, online_tier: TicketTier, seats: list[VenueSeat]
    ) -> None:
        """A seat outside the tier's sector would be priced as GA and disagree with checkout."""
        stray = VenueSeat.objects.create(
            sector=VenueSector.objects.create(
                venue=seats[0].sector.venue,
                name="Balcony",
                shape=[{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}, {"x": 0, "y": 1}],
            ),
            label="B1",
            row_label="B",
            number=1,
            position={"x": 0, "y": 1},
            is_active=True,
        )
        with pytest.raises(HttpError) as exc:
            _preview(seated_event, online_tier, seats=[stray])
        assert exc.value.status_code == 400


class TestDiscountsOnAMixedCart:
    """The code applies per seat, exactly as ``build_batch_pricing`` applies it at checkout."""

    def test_percentage_code_discounts_each_category_on_its_own_price(
        self, seated_event: Event, online_tier: TicketTier, seats: list[VenueSeat], pct10: DiscountCode
    ) -> None:
        """10% off 80/30/50 → 72/27/45, and the lines stay split by category."""
        result = _preview(seated_event, online_tier, seats=seats, discount_code="PCT10")

        assert _quote(result.line_items) == [
            ("Premium", 1, Decimal("72.00")),
            ("Standard", 1, Decimal("27.00")),
            (None, 1, Decimal("45.00")),
        ]
        assert result.total_gross == Decimal("144.00")

    def test_fixed_amount_code_can_split_one_category_from_another(
        self, seated_event: Event, online_tier: TicketTier, seats: list[VenueSeat], vat_org: Organization
    ) -> None:
        """A 40.00 code floors the 30.00 Standard seat at zero — 40/0/10, three distinct prices."""
        DiscountCode.objects.create(
            code="FLAT40",
            organization=vat_org,
            discount_type=DiscountCode.DiscountType.FIXED_AMOUNT,
            discount_value=Decimal("40.00"),
            currency="EUR",
            max_uses_per_user=10,
        )
        result = _preview(seated_event, online_tier, seats=seats, discount_code="FLAT40")

        assert _quote(result.line_items) == [
            ("Premium", 1, Decimal("40.00")),
            ("Standard", 1, Decimal("0.00")),
            (None, 1, Decimal("10.00")),
        ]
        assert result.total_gross == Decimal("50.00")

    def test_invalid_code_falls_back_to_list_prices(
        self, seated_event: Event, online_tier: TicketTier, seats: list[VenueSeat]
    ) -> None:
        """Pre-existing behaviour: the buyer is still typing, so a bad code is ignored."""
        result = _preview(seated_event, online_tier, seats=seats, discount_code="NOPE")

        assert result.total_gross == GROSS_TOTAL

    def test_code_below_its_minimum_purchase_is_ignored_rather_than_quoted(
        self, seated_event: Event, online_tier: TicketTier, seats: list[VenueSeat], vat_org: Organization
    ) -> None:
        """Checkout would 400 on this code (spec §5.6); quoting its discount would be a lie."""
        DiscountCode.objects.create(
            code="BIGSPEND",
            organization=vat_org,
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("50.00"),
            currency="EUR",
            min_purchase_amount=Decimal("500.00"),
            max_uses_per_user=10,
        )
        result = _preview(seated_event, online_tier, seats=seats, discount_code="BIGSPEND")

        assert result.total_gross == GROSS_TOTAL


class TestDriftedCategory:
    """A seat painted into a category the tier does not price."""

    def test_preview_refuses_exactly_as_checkout_does(
        self, seated_event: Event, online_tier: TicketTier, seats: list[VenueSeat], sector: VenueSector
    ) -> None:
        """400 naming the category — the same refusal, at the cheapest possible moment.

        Softening this into a warning was considered and rejected: the alternative to
        refusing is quoting a total for a seat that cannot be sold, which is the silent
        preview/charge disagreement this whole task exists to remove. The buyer gets an
        actionable message (pick another seat) instead of a surprise at Stripe.
        """
        drifted_category = PriceCategory.objects.create(venue=sector.venue, name="Box", color="#0000aa")
        drifted = VenueSeat.objects.create(
            sector=sector,
            label="A9",
            row_label="A",
            number=9,
            position={"x": 8, "y": 0},
            is_active=True,
            default_price_category=drifted_category,
        )

        with pytest.raises(HttpError) as preview_exc:
            _preview(seated_event, online_tier, seats=[drifted])

        with pytest.raises(HttpError) as checkout_exc:
            BatchTicketService(seated_event, online_tier, RevelUser.objects.first()).create_batch(  # type: ignore[arg-type]
                [TicketPurchaseItem(guest_name="Guest", seat_id=drifted.pk)]
            )

        assert preview_exc.value.status_code == 400
        assert "Box" in str(preview_exc.value.message)
        assert str(preview_exc.value.message) == str(checkout_exc.value.message)


class TestWireShape:
    """The request and response shapes as the frontend actually sees them."""

    def test_seat_ids_round_trip_through_the_endpoint(
        self, seated_event: Event, online_tier: TicketTier, seats: list[VenueSeat], member_user: RevelUser
    ) -> None:
        """POST seat_ids, get back one named line per distinct price."""
        from django.test import Client
        from django.urls import reverse
        from ninja_jwt.tokens import RefreshToken

        refresh = RefreshToken.for_user(member_user)
        client = Client(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")  # type: ignore[attr-defined]

        response = client.post(
            reverse("api:vat_preview", kwargs={"event_id": str(seated_event.pk)}),
            data=orjson.dumps(
                {
                    "billing_info": {"billing_name": "ACME SRL", "vat_country_code": "IT"},
                    "items": [
                        {
                            "tier_id": str(online_tier.pk),
                            "count": 3,
                            "seat_ids": [str(seat.pk) for seat in seats],
                        }
                    ],
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert [
            (li["price_category_name"], li["ticket_count"], li["unit_price_gross"]) for li in data["line_items"]
        ] == [
            ("Premium", 1, "80.00"),
            ("Standard", 1, "30.00"),
            (None, 1, "50.00"),
        ]
        assert data["total_gross"] == "160.00"


class TestBestAvailableZonePreview:
    """A best-available line is priced by the zone it asks for, with no seats at all.

    The buyer has picked nothing — the picker assigns seats at hold/checkout time — so the
    only thing that fixes the price is the requested price category. One zone per request
    makes the line uniformly priced by construction, which is exactly why it is quotable
    without seats.
    """

    def test_zone_priced_line_quotes_the_zone_price(
        self, seated_event: Event, ba_tier: TicketTier, categories: tuple[PriceCategory, PriceCategory]
    ) -> None:
        """Two Premium seats, unchosen: 2 × 80.00, named "Premium", 22% VAT on the real total."""
        premium, _standard = categories

        result = _preview(seated_event, ba_tier, count=2, zone=premium)

        assert _quote(result.line_items) == [("Premium", 2, PREMIUM)]
        assert result.total_gross == Decimal("160.00")
        # 80.00 gross at 22% inclusive → 65.57 net + 14.43 VAT, twice.
        assert result.line_items[0].unit_price_net == Decimal("65.57")
        assert result.line_items[0].unit_vat == Decimal("14.43")
        assert result.total_net == Decimal("131.14")
        assert result.total_vat == Decimal("28.86")
        assert result.total_net + result.total_vat == result.total_gross

    def test_the_other_zone_of_the_same_tier_prices_differently(
        self, seated_event: Event, ba_tier: TicketTier, categories: tuple[PriceCategory, PriceCategory]
    ) -> None:
        """The zone is the whole price — the same tier quotes 30.00 for Standard."""
        _premium, standard = categories

        result = _preview(seated_event, ba_tier, count=2, zone=standard)

        assert _quote(result.line_items) == [("Standard", 2, STANDARD)]
        assert result.total_gross == Decimal("60.00")

    def test_non_eu_buyer_is_zero_rated_on_a_zone_line(
        self, seated_event: Event, ba_tier: TicketTier, categories: tuple[PriceCategory, PriceCategory]
    ) -> None:
        """VAT is applied to the zone price, not the tier's flat one."""
        premium, _standard = categories

        result = _preview(seated_event, ba_tier, count=2, zone=premium, billing=_non_eu())

        assert result.line_items[0].line_gross == Decimal("131.14")  # 2 × 65.57
        assert result.total_vat == ZERO
        assert result.reverse_charge is False

    def test_discount_code_applies_to_the_zone_price(
        self,
        seated_event: Event,
        ba_tier: TicketTier,
        categories: tuple[PriceCategory, PriceCategory],
        pct10: DiscountCode,
    ) -> None:
        """10% off Premium's 80.00 → 72.00, through the same pricing authority as checkout."""
        premium, _standard = categories

        result = _preview(seated_event, ba_tier, count=2, zone=premium, discount_code="PCT10")

        assert _quote(result.line_items) == [("Premium", 2, Decimal("72.00"))]
        assert result.total_gross == Decimal("144.00")

    def test_unmapped_best_available_tier_is_flat_priced_without_a_zone(
        self, seated_event: Event, ba_unmapped_tier: TicketTier
    ) -> None:
        """No map means no zones: the flat tier price, one anonymous line, as before."""
        result = _preview(seated_event, ba_unmapped_tier, count=3)

        assert _quote(result.line_items) == [(None, 3, FLAT)]
        assert result.total_gross == Decimal("150.00")


class TestZoneValidation:
    """The refusals, which must match checkout's zone resolution exactly (Task B, #749)."""

    def test_mapped_best_available_tier_without_a_zone_is_refused(
        self, seated_event: Event, ba_tier: TicketTier
    ) -> None:
        """Quoting the flat 50.00 for a tier that sells at 80/30 would be a lie; name the zones."""
        with pytest.raises(HttpError) as exc:
            _preview(seated_event, ba_tier, count=2)

        assert exc.value.status_code == 400
        assert "Premium" in str(exc.value.message)
        assert "Standard" in str(exc.value.message)

    def test_zone_outside_the_tiers_map_is_refused(
        self, seated_event: Event, ba_tier: TicketTier, unpriced_zone: PriceCategory
    ) -> None:
        """A category the tier does not price is not a sellable zone — same message."""
        with pytest.raises(HttpError) as exc:
            _preview(seated_event, ba_tier, count=2, zone=unpriced_zone)

        assert exc.value.status_code == 400
        assert "Premium" in str(exc.value.message)
        assert "Standard" in str(exc.value.message)

    def test_zone_on_an_unmapped_tier_is_refused(
        self, seated_event: Event, ba_unmapped_tier: TicketTier, categories: tuple[PriceCategory, PriceCategory]
    ) -> None:
        """The tier sells one flat price; honouring a zone would quote a price it has not set."""
        premium, _standard = categories

        with pytest.raises(HttpError) as exc:
            _preview(seated_event, ba_unmapped_tier, count=2, zone=premium)

        assert exc.value.status_code == 400
        assert "does not sell by zone" in str(exc.value.message)

    def test_zone_on_a_user_choice_tier_is_refused(
        self, seated_event: Event, online_tier: TicketTier, categories: tuple[PriceCategory, PriceCategory]
    ) -> None:
        """A user-choice line prices from its seats; a zone would quote seats it never checked."""
        premium, _standard = categories

        with pytest.raises(HttpError) as exc:
            _preview(seated_event, online_tier, count=2, zone=premium)

        assert exc.value.status_code == 400
        assert "does not sell by zone" in str(exc.value.message)

    def test_seats_and_a_zone_together_are_refused(
        self,
        seated_event: Event,
        ba_tier: TicketTier,
        seats: list[VenueSeat],
        categories: tuple[PriceCategory, PriceCategory],
    ) -> None:
        """Two selectors that can disagree: pick one rather than silently preferring either."""
        premium, _standard = categories

        with pytest.raises(HttpError) as exc:
            _preview(seated_event, ba_tier, seats=[seats[0]], zone=premium)

        assert exc.value.status_code == 400
        assert "not both" in str(exc.value.message)


class TestModeAgnosticResponseShape:
    """The frontend must not branch on seating mode to render a quote."""

    def test_zone_preview_is_shaped_exactly_like_the_seat_preview_that_prices_the_same(
        self,
        seated_event: Event,
        online_tier: TicketTier,
        ba_tier: TicketTier,
        seats: list[VenueSeat],
        sector: VenueSector,
        categories: tuple[PriceCategory, PriceCategory],
    ) -> None:
        """Two Premium seats picked by hand vs two asked for by zone: the same line, field for field.

        Only ``tier_name`` may differ (they are different tiers). Everything the buyer
        reads — category, count, unit and line money, VAT rate — is byte-identical.
        """
        premium, _standard = categories
        premium_twin = VenueSeat.objects.create(
            sector=sector,
            label="A4",
            row_label="A",
            number=4,
            position={"x": 3, "y": 0},
            is_active=True,
            default_price_category=premium,
        )

        by_seat = _preview(seated_event, online_tier, seats=[seats[0], premium_twin])
        by_zone = _preview(seated_event, ba_tier, count=2, zone=premium)

        anonymize = [dataclasses.replace(li, tier_name="") for li in by_seat.line_items]
        assert anonymize == [dataclasses.replace(li, tier_name="") for li in by_zone.line_items]
        assert (by_seat.total_net, by_seat.total_vat, by_seat.total_gross) == (
            by_zone.total_net,
            by_zone.total_vat,
            by_zone.total_gross,
        )

    def test_zone_round_trips_through_the_endpoint(
        self,
        seated_event: Event,
        ba_tier: TicketTier,
        categories: tuple[PriceCategory, PriceCategory],
        member_user: RevelUser,
    ) -> None:
        """POST price_category_id, get back the same named-line payload seats produce."""
        from django.test import Client
        from django.urls import reverse
        from ninja_jwt.tokens import RefreshToken

        premium, _standard = categories
        refresh = RefreshToken.for_user(member_user)
        client = Client(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")  # type: ignore[attr-defined]

        response = client.post(
            reverse("api:vat_preview", kwargs={"event_id": str(seated_event.pk)}),
            data=orjson.dumps(
                {
                    "billing_info": {"billing_name": "ACME SRL", "vat_country_code": "IT"},
                    "items": [{"tier_id": str(ba_tier.pk), "count": 2, "price_category_id": str(premium.pk)}],
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert [
            (li["price_category_name"], li["ticket_count"], li["unit_price_gross"]) for li in data["line_items"]
        ] == [("Premium", 2, "80.00")]
        assert data["total_gross"] == "160.00"


class TestPreviewMatchesTheCharge:
    """The point of the task: the quote and the charge are the same number."""

    @pytest.mark.parametrize("billing", [_domestic(), _non_eu()], ids=["domestic-b2c", "non-eu-export"])
    def test_preview_total_equals_the_sum_of_the_payment_rows(
        self,
        seated_event: Event,
        online_tier: TicketTier,
        seats: list[VenueSeat],
        member_user: RevelUser,
        billing: BuyerBillingInfoSchema,
    ) -> None:
        """Preview the mixed cart, then actually buy it, and compare against Stripe's inputs.

        ``Payment.amount`` is what each Stripe line item charges (one row per ticket),
        so its sum is the buyer's real total. Before this change the preview reported
        3 × 50.00 for a cart that charges 80 + 30 + 50.
        """
        preview = _preview(seated_event, online_tier, seats=seats, billing=billing)

        result = BatchTicketService(seated_event, online_tier, member_user).create_batch(
            [TicketPurchaseItem(guest_name=f"Guest {i}", seat_id=seat.pk) for i, seat in enumerate(seats)],
            billing_info=billing,
        )
        tickets = result[0] if isinstance(result, tuple) else result
        payments = Payment.objects.filter(ticket__in=tickets)

        assert preview.total_gross == sum(payment.amount for payment in payments)
        assert preview.total_net == sum((payment.net_amount or ZERO for payment in payments), ZERO)
        assert preview.total_vat == sum((payment.vat_amount or ZERO for payment in payments), ZERO)
        # And line by line: every previewed line is a price actually charged, at that
        # quantity — not just a total that happens to reconcile.
        assert sorted(li.line_gross for li in preview.line_items) == sorted(p.amount for p in payments)

    def test_preview_total_equals_price_paid_on_a_discounted_offline_cart(
        self,
        seated_event: Event,
        vat_org: Organization,
        sector: VenueSector,
        categories: tuple[PriceCategory, PriceCategory],
        seats: list[VenueSeat],
        member_user: RevelUser,
        pct10: DiscountCode,
    ) -> None:
        """Offline stamps ``price_paid`` per ticket; the discounted preview must match it."""
        tier = make_category_tier(seated_event, sector, categories, TicketTier.PaymentMethod.OFFLINE)

        preview = _preview(seated_event, tier, seats=seats, discount_code="PCT10")

        result = BatchTicketService(seated_event, tier, member_user, discount_code=pct10).create_batch(
            [TicketPurchaseItem(guest_name=f"Guest {i}", seat_id=seat.pk) for i, seat in enumerate(seats)]
        )
        tickets = t.cast(list[Ticket], result)

        assert preview.total_gross == sum((ticket.price_paid or ZERO for ticket in tickets), ZERO)
        assert preview.total_gross == Decimal("144.00")
