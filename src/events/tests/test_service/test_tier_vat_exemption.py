"""A 0% VAT tier of an invoicing organization needs a VAT exemption reason (EN 16931 category E)."""

from decimal import Decimal

import pytest
from ninja.errors import HttpError

from events.models import Event, Organization, PriceCategory, TicketTier, Venue, VenueSector
from events.schema import TicketTierCreateSchema, TicketTierUpdateSchema
from events.service import ticket_service
from events.utils.vat_category import effective_vat_exemption_reason, is_paid_online

pytestmark = pytest.mark.django_db

MESSAGE = "A VAT exemption reason is required for a ticket tier with a 0% VAT rate"


@pytest.fixture
def invoicing_org(event: Event) -> Organization:
    """The event's organization, invoicing automatically with a 20% default rate."""
    org = event.organization
    org.invoicing_mode = Organization.InvoicingMode.AUTO
    org.vat_rate = Decimal("20")
    org.stripe_account_id = "acct_test_123"
    org.stripe_charges_enabled = True
    org.stripe_details_submitted = True
    org.platform_fee_percent = Decimal("0")
    org.platform_fee_fixed = Decimal("0")
    org.save()
    return org


def _create(event: Event, **kwargs: object) -> TicketTier:
    """Create a paid online tier unless told otherwise (only those are invoiced)."""
    fields: dict[str, object] = {"payment_method": TicketTier.PaymentMethod.ONLINE, "price": Decimal("10")}
    return ticket_service.create_ticket_tier(
        event,
        TicketTierCreateSchema(name="Zero", **{**fields, **kwargs}),  # type: ignore[arg-type]
    )


def test_zero_rate_tier_requires_reason_when_invoicing(event: Event, invoicing_org: Organization) -> None:
    with pytest.raises(HttpError) as exc_info:
        _create(event, vat_rate=Decimal("0"))
    assert exc_info.value.status_code == 422
    assert MESSAGE in str(exc_info.value)


def test_zero_rate_tier_accepts_tier_reason(event: Event, invoicing_org: Organization) -> None:
    tier = _create(event, vat_rate=Decimal("0"), vat_exemption_reason="§4 Nr. 20 UStG")
    assert tier.vat_exemption_reason == "§4 Nr. 20 UStG"


def test_zero_rate_tier_accepts_org_default_reason(event: Event, invoicing_org: Organization) -> None:
    invoicing_org.vat_exemption_reason = "Art. 132 VAT Directive"
    invoicing_org.save()
    tier = _create(event, vat_rate=Decimal("0"))
    assert tier.pk is not None


def test_org_zero_default_rate_tier_inherits_requirement(event: Event, invoicing_org: Organization) -> None:
    invoicing_org.vat_rate = Decimal("0")
    invoicing_org.save()
    with pytest.raises(HttpError) as exc_info:
        _create(event)
    assert exc_info.value.status_code == 422


def test_zero_rate_tier_without_reason_ok_when_not_invoicing(event: Event, invoicing_org: Organization) -> None:
    invoicing_org.invoicing_mode = Organization.InvoicingMode.NONE
    invoicing_org.save()
    assert _create(event, vat_rate=Decimal("0")).pk is not None


def test_update_to_zero_rate_requires_reason(event: Event, invoicing_org: Organization) -> None:
    tier = _create(event, vat_rate=Decimal("10"))
    with pytest.raises(HttpError) as exc_info:
        ticket_service.update_ticket_tier(
            tier,
            TicketTierUpdateSchema(vat_rate=Decimal("0")),  # type: ignore[call-arg]
        )
    assert exc_info.value.status_code == 422
    assert MESSAGE in str(exc_info.value)

    updated = ticket_service.update_ticket_tier(
        tier,
        TicketTierUpdateSchema(vat_rate=Decimal("0"), vat_exemption_reason="Exempt"),  # type: ignore[call-arg]
    )
    assert updated.vat_exemption_reason == "Exempt"


def test_effective_reason_prefers_tier(event: Event, invoicing_org: Organization) -> None:
    invoicing_org.vat_exemption_reason = " org reason "
    tier = TicketTier(event=event, vat_exemption_reason=" tier reason ")
    assert effective_vat_exemption_reason(tier, invoicing_org) == "tier reason"
    tier.vat_exemption_reason = "  "
    assert effective_vat_exemption_reason(tier, invoicing_org) == "org reason"
    invoicing_org.vat_exemption_reason = ""
    assert effective_vat_exemption_reason(tier, invoicing_org) == ""


def _legacy_zero_tier(event: Event, org: Organization) -> TicketTier:
    """A 0% tier without a reason, as created before the rule existed (bypasses the service)."""
    org.invoicing_mode = Organization.InvoicingMode.AUTO
    org.save()
    return TicketTier.objects.create(
        event=event,
        name="Legacy",
        price=10,
        payment_method=TicketTier.PaymentMethod.ONLINE,
        vat_rate=Decimal("0"),
    )


def test_unrelated_update_of_legacy_zero_tier_succeeds(event: Event, invoicing_org: Organization) -> None:
    tier = _legacy_zero_tier(event, invoicing_org)
    updated = ticket_service.update_ticket_tier(tier, TicketTierUpdateSchema(name="Renamed"))  # type: ignore[call-arg]
    assert updated.name == "Renamed"


@pytest.mark.parametrize("field", ["vat_exemption_reason", "vat_rate"])
def test_vat_touching_update_of_legacy_zero_tier_still_422(
    event: Event, invoicing_org: Organization, field: str
) -> None:
    tier = _legacy_zero_tier(event, invoicing_org)
    value = "" if field == "vat_exemption_reason" else Decimal("0")
    with pytest.raises(HttpError) as exc_info:
        ticket_service.update_ticket_tier(tier, TicketTierUpdateSchema(**{field: value}))  # type: ignore[arg-type]
    assert exc_info.value.status_code == 422


def test_free_zero_rate_tier_needs_no_reason(event: Event, invoicing_org: Organization) -> None:
    invoicing_org.vat_rate = Decimal("0")
    invoicing_org.save()
    free = _create(event, payment_method=TicketTier.PaymentMethod.FREE, price=Decimal("0"))
    assert free.pk is not None


def test_offline_paid_zero_rate_tier_needs_no_reason(event: Event, invoicing_org: Organization) -> None:
    invoicing_org.vat_rate = Decimal("0")
    invoicing_org.save()
    offline = _create(event, payment_method=TicketTier.PaymentMethod.OFFLINE)
    assert offline.pk is not None


def test_online_pwyc_zero_rate_tier_requires_reason(event: Event, invoicing_org: Organization) -> None:
    with pytest.raises(HttpError) as exc_info:
        _create(event, vat_rate=Decimal("0"), price_type=TicketTier.PriceType.PWYC)
    assert exc_info.value.status_code == 422


def test_switching_zero_rate_tier_to_online_requires_reason(event: Event, invoicing_org: Organization) -> None:
    tier = _create(event, vat_rate=Decimal("0"), payment_method=TicketTier.PaymentMethod.OFFLINE)
    with pytest.raises(HttpError) as exc_info:
        ticket_service.update_ticket_tier(
            tier,
            TicketTierUpdateSchema(payment_method=TicketTier.PaymentMethod.ONLINE, price=Decimal("10")),  # type: ignore[call-arg]
        )
    assert exc_info.value.status_code == 422
    assert MESSAGE in str(exc_info.value)


def test_price_change_of_legacy_online_zero_rate_tier_without_reason_is_422(
    event: Event, invoicing_org: Organization
) -> None:
    tier = _create(
        event, vat_rate=Decimal("0"), payment_method=TicketTier.PaymentMethod.ONLINE, vat_exemption_reason="x"
    )
    tier.vat_exemption_reason = ""
    tier.save()
    with pytest.raises(HttpError):
        ticket_service.update_ticket_tier(tier, TicketTierUpdateSchema(price=Decimal("20")))  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"payment_method": "online", "price": "10"}, True),
        ({"payment_method": "online", "price": "0"}, False),
        ({"payment_method": "offline", "price": "10"}, False),
        ({"payment_method": "free", "price": "0"}, False),
        ({"payment_method": "online", "price": "0", "price_type": "pwyc", "pwyc_min": "1", "pwyc_max": None}, True),
        ({"payment_method": "online", "price": "0", "price_type": "pwyc", "pwyc_min": "0", "pwyc_max": "5"}, True),
        ({"payment_method": "online", "price": "0", "price_type": "pwyc", "pwyc_min": "0", "pwyc_max": None}, False),
    ],
)
def test_is_paid_online(fields: dict[str, str | None], expected: bool) -> None:
    assert is_paid_online(TicketTier(**fields)) is expected


# --- seated tiers priced only through category_prices (#1247) ---


@pytest.fixture
def category(event: Event) -> PriceCategory:
    venue = Venue.objects.create(organization=event.organization, name="Theatre", capacity=100)
    return PriceCategory.objects.create(venue=venue, name="Premium", color="#aa0000", display_order=0)


def _online_seated(event: Event, category: PriceCategory, **kwargs: object) -> TicketTier:
    """A seated ONLINE tier with base price 0 and a 25.00 category price.

    Created through the ORM: the create/update schemas force an ONLINE base price >= 1, so such a tier
    only exists as legacy/imported data, which is exactly what the guard must still catch.
    """
    sector = VenueSector.objects.create(venue=category.venue, name="Stalls")
    return TicketTier.objects.create(
        event=event,
        name="Seated",
        price=Decimal("0"),
        payment_method=TicketTier.PaymentMethod.ONLINE,
        venue=category.venue,
        sector=sector,
        seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
        category_prices={str(category.id): "25.00"},
        vat_rate=Decimal("10"),
        **kwargs,
    )


def test_seated_category_priced_zero_rate_tier_requires_reason(
    event: Event, invoicing_org: Organization, category: PriceCategory
) -> None:
    tier = _online_seated(event, category)
    with pytest.raises(HttpError) as exc_info:
        ticket_service.update_ticket_tier(tier, TicketTierUpdateSchema(vat_rate=Decimal("0")))  # type: ignore[call-arg]
    assert exc_info.value.status_code == 422
    assert MESSAGE in str(exc_info.value)


def test_seated_category_priced_zero_rate_tier_with_reason_is_saved(
    event: Event, invoicing_org: Organization, category: PriceCategory
) -> None:
    tier = _online_seated(event, category)
    updated = ticket_service.update_ticket_tier(
        tier,
        TicketTierUpdateSchema(vat_rate=Decimal("0"), vat_exemption_reason="Art. 132"),  # type: ignore[call-arg]
    )
    assert updated.vat_rate == Decimal("0")


def test_category_prices_update_triggers_the_recheck(
    event: Event, invoicing_org: Organization, category: PriceCategory
) -> None:
    tier = _online_seated(event, category)
    TicketTier.objects.filter(pk=tier.pk).update(vat_rate=Decimal("0"))
    tier.refresh_from_db()
    with pytest.raises(HttpError):
        ticket_service.update_ticket_tier(
            tier,
            TicketTierUpdateSchema(category_prices={str(category.id): "30.00"}),  # type: ignore[call-arg]
        )


def test_is_paid_online_with_category_prices() -> None:
    base: dict[str, object] = {"payment_method": "online", "price": "0"}
    cid = "6f1c1d0a-0000-4000-8000-000000000001"
    assert is_paid_online(TicketTier(**base, category_prices={cid: "25.00"})) is True
    assert is_paid_online(TicketTier(**base, category_prices={cid: "0.00"})) is False
    assert is_paid_online(TicketTier(**base, category_prices={})) is False
    assert is_paid_online(TicketTier(**base, category_prices=None)) is False


def test_is_paid_online_pwyc_tier_with_category_prices() -> None:
    """An ORM-written PWYC tier with a priced category still sells online (#1242)."""
    cid = "6f1c1d0a-0000-4000-8000-000000000001"
    fields: dict[str, object] = {"payment_method": "online", "price": "0", "price_type": "pwyc", "pwyc_min": "0"}
    assert is_paid_online(TicketTier(**fields, category_prices={cid: "25.00"})) is True
    assert is_paid_online(TicketTier(**fields, category_prices={})) is False


def _paused_legacy_zero_tier(event: Event, org: Organization) -> TicketTier:
    tier = _legacy_zero_tier(event, org)
    tier.sales_paused = True
    tier.save()
    return tier


def test_resuming_paused_zero_rate_tier_without_reason_is_422(event: Event, invoicing_org: Organization) -> None:
    tier = _paused_legacy_zero_tier(event, invoicing_org)
    with pytest.raises(HttpError) as exc_info:
        ticket_service.update_ticket_tier(tier, TicketTierUpdateSchema(sales_paused=False))  # type: ignore[call-arg]
    assert exc_info.value.status_code == 422
    assert MESSAGE in exc_info.value.message


def test_resuming_paused_zero_rate_tier_with_reason_succeeds(event: Event, invoicing_org: Organization) -> None:
    tier = _paused_legacy_zero_tier(event, invoicing_org)
    tier.vat_exemption_reason = "Art. 10 UStG"
    tier.save()
    updated = ticket_service.update_ticket_tier(tier, TicketTierUpdateSchema(sales_paused=False))  # type: ignore[call-arg]
    assert updated.sales_paused is False


def test_pausing_zero_rate_tier_without_reason_succeeds(event: Event, invoicing_org: Organization) -> None:
    tier = _legacy_zero_tier(event, invoicing_org)
    updated = ticket_service.update_ticket_tier(tier, TicketTierUpdateSchema(sales_paused=True))  # type: ignore[call-arg]
    assert updated.sales_paused is True


def test_sql_zero_rate_guard_agrees_with_is_paid_online_for_pwyc_category_prices(
    event: Event, invoicing_org: Organization
) -> None:
    """A PWYC tier with a priced category is paid online for the Python rule and the SQL guard alike (#1242)."""
    from events.service.zero_rate_guard_service import zero_rate_tiers_missing_reason

    cid = "6f1c1d0a-0000-4000-8000-000000000001"
    tier = TicketTier(  # the API forbids this combination, so it is written around validation
        event=event,
        name="P",
        payment_method=TicketTier.PaymentMethod.ONLINE,
        price=0,
        price_type=TicketTier.PriceType.PWYC,
        pwyc_min=0,
        vat_rate=Decimal("0"),
        category_prices={cid: "25.00"},
    )
    TicketTier.objects.bulk_create([tier])
    assert is_paid_online(tier) is True
    assert zero_rate_tiers_missing_reason(invoicing_org) is True
