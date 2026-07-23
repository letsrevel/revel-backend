"""Pro-rata platform fee attribution on a mixed-price cart (#753).

The fee is computed on the true cart total and then written onto one ``Payment`` row
per ticket. Splitting it evenly was invisible while every ticket in a batch cost the
same; per-seat-category pricing (#739) made an 80 + 20 cart ordinary, and both fee
consumers — the org's platform-fee invoice and the referral payout — aggregate over
``SUCCEEDED`` rows only. A refund flips one row to ``REFUNDED``, so whatever share it
carried leaves the total with it: an even split then bills 2.50 against 80.00 retained
(3.125% of a 5% fee) or 2.50 against 20.00 (12.5%).
"""

from decimal import Decimal
from unittest import mock
from uuid import uuid4

import pytest
from django.utils import timezone

from accounts.models import Referral, ReferralCode, ReferralPayout, RevelUser
from common.models import SiteSettings
from events.models import Event, Organization, Payment, Ticket, TicketTier
from events.models.invoice import PlatformFeeInvoice
from events.service import stripe_service
from events.service.invoice_service import generate_invoices_for_period
from events.service.referral_payout_service import calculate_payouts_for_period
from events.service.seating.pricing import TicketPrice

pytestmark = pytest.mark.django_db


@pytest.fixture
def fee_organization(organization: Organization) -> Organization:
    """Stripe-connected org billed a flat 5% platform fee, no fixed component."""
    organization.stripe_account_id = "acct_prorata"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.platform_fee_percent = Decimal("5.00")
    organization.platform_fee_fixed = Decimal("0.00")
    organization.billing_email = "billing@prorata.test"
    organization.vat_country_code = "IT"
    organization.save()
    return organization


@pytest.fixture
def site_settings() -> SiteSettings:
    """Platform business identity the invoice snapshot requires, with fee VAT on top."""
    site = SiteSettings.get_solo()
    site.platform_business_name = "Revel S.r.l."
    site.platform_business_address = "Via Roma 1, 00100 Roma, Italy"
    site.platform_vat_id = "IT12345678901"
    site.platform_vat_country = "IT"
    site.platform_vat_rate = Decimal("22.00")
    site.save()
    return site


@pytest.fixture
def fee_tier(event: Event, fee_organization: Organization) -> TicketTier:
    """A paid tier on the fee-charging org's event."""
    event.organization = fee_organization
    event.save()
    tier = event.ticket_tiers.first()
    assert tier is not None
    tier.price = Decimal("20.00")
    tier.total_quantity = 20
    tier.save()
    return tier


def _reserve(
    event: Event,
    tier: TicketTier,
    user: RevelUser,
    named_prices: list[tuple[str, str]],
) -> list[Payment]:
    """Reserve a cart of ``(guest_name, price)`` pairs and return its Payment rows."""
    tickets = [
        Ticket.objects.create(event=event, tier=tier, user=user, status=Ticket.TicketStatus.PENDING, guest_name=name)
        for name, _ in named_prices
    ]
    reservation_id = uuid4()
    stripe_service.reserve_batch_payments(
        event=event,
        tier=tier,
        user=user,
        tickets=tickets,
        reservation_id=reservation_id,
        lines=[TicketPrice(unit_price=Decimal(price), discount_amount=Decimal("0.00")) for _, price in named_prices],
    )
    return list(Payment.objects.filter(reservation_id=reservation_id).select_related("ticket"))


def _by_guest(payments: list[Payment]) -> dict[str, Payment]:
    return {payment.ticket.guest_name: payment for payment in payments}


def _succeed(payments: list[Payment]) -> None:
    Payment.objects.filter(pk__in=[p.pk for p in payments]).update(status=Payment.PaymentStatus.SUCCEEDED)


def _refund(payment: Payment) -> None:
    """Mirror what the refund webhook does to a row (``stripe_webhooks`` line 762)."""
    Payment.objects.filter(pk=payment.pk).update(status=Payment.PaymentStatus.REFUNDED)


def _invoice_for(org: Organization) -> PlatformFeeInvoice:
    """Run the monthly invoice generator over today and return this org's invoice."""
    today = timezone.localdate()
    with mock.patch("events.service.invoice_service.render_invoice_pdf", return_value=b"%PDF-"):
        generate_invoices_for_period(today, today)
    return PlatformFeeInvoice.objects.get(organization=org)


class TestProRataAttribution:
    """The fee written on each row must follow the revenue that row represents."""

    def test_mixed_cart_splits_the_fee_by_price(
        self, event: Event, fee_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        """80 + 20 at 5%: a 5.00 fee lands as 4.00 + 1.00, not 2.50 + 2.50."""
        cart = [("Premium", "80.00"), ("Galleria", "20.00")]
        payments = _by_guest(_reserve(event, fee_tier, organization_owner_user, cart))

        assert payments["Premium"].platform_fee == Decimal("4.00")
        assert payments["Galleria"].platform_fee == Decimal("1.00")

    def test_batch_total_is_unchanged(
        self, event: Event, fee_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        """Whatever the split, the batch still carries exactly the fee on the cart total."""
        payments = _reserve(event, fee_tier, organization_owner_user, [("A", "80.00"), ("B", "20.00"), ("C", "12.34")])

        gross = sum((p.platform_fee for p in payments), Decimal("0"))
        net = sum((p.platform_fee_net or Decimal("0") for p in payments), Decimal("0"))
        vat = sum((p.platform_fee_vat or Decimal("0") for p in payments), Decimal("0"))
        # 5% of 112.34 = 5.62 (ROUND_HALF_UP)
        assert gross == Decimal("5.62")
        assert net + vat == gross

    def test_net_and_vat_are_split_too(
        self, event: Event, fee_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        """The referral payout reads ``platform_fee_net``, so it must be weighted as well."""
        cart = [("Premium", "80.00"), ("Galleria", "20.00")]
        payments = _by_guest(_reserve(event, fee_tier, organization_owner_user, cart))
        premium, galleria = payments["Premium"], payments["Galleria"]

        assert premium.platform_fee_net is not None and galleria.platform_fee_net is not None
        assert premium.platform_fee_net == premium.platform_fee - (premium.platform_fee_vat or Decimal("0"))
        # The 80.00 row carries four times the 20.00 row's net fee, as its revenue does.
        assert premium.platform_fee_net == galleria.platform_fee_net * 4

    def test_zero_priced_row_carries_no_fee(
        self, event: Event, fee_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        """A ticket a fixed-amount discount floored to 0.00 earns the platform nothing."""
        cart = [("Paid", "80.00"), ("Freebie", "0.00")]
        payments = _by_guest(_reserve(event, fee_tier, organization_owner_user, cart))

        assert payments["Freebie"].platform_fee == Decimal("0.00")
        assert payments["Freebie"].platform_fee_net == Decimal("0.00")
        assert payments["Freebie"].platform_fee_vat == Decimal("0.00")
        assert payments["Paid"].platform_fee == Decimal("4.00")

    def test_uniform_cart_keeps_the_even_split(
        self, event: Event, fee_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        """Every flat-price cart must record exactly what it recorded before #753.

        3 x 33.33 = 99.99 -> a 5.00 fee whose even split rounds *up* (1.67 x 3 = 5.01)
        and hands a penny back off the first row: 1.66 / 1.67 / 1.67. A plain
        largest-remainder pass would have produced 1.67 / 1.67 / 1.66 instead, so this
        pins the remainder ordering, not just the multiset of values.
        """
        payments = _reserve(event, fee_tier, organization_owner_user, [("A", "33.33"), ("B", "33.33"), ("C", "33.33")])
        by_guest = _by_guest(payments)

        assert by_guest["A"].platform_fee == Decimal("1.66")
        assert by_guest["B"].platform_fee == Decimal("1.67")
        assert by_guest["C"].platform_fee == Decimal("1.67")
        assert sum((p.platform_fee for p in payments), Decimal("0")) == Decimal("5.00")


class TestRefundedMixedCartBillsCorrectly:
    """The issue's scenario, end to end, through both consumers."""

    @pytest.fixture
    def cart(
        self,
        site_settings: SiteSettings,
        event: Event,
        fee_tier: TicketTier,
        organization_owner_user: RevelUser,
    ) -> dict[str, Payment]:
        """An 80 + 20 cart, both tickets paid for."""
        payments = _reserve(event, fee_tier, organization_owner_user, [("Premium", "80.00"), ("Galleria", "20.00")])
        _succeed(payments)
        return _by_guest(payments)

    @pytest.fixture
    def referral(self, fee_organization: Organization, django_user_model: type[RevelUser]) -> Referral:
        """A referrer on 100% revenue share, so the payout *is* the net fee."""
        referrer = django_user_model.objects.create_user(
            username="fee_referrer", email="fee_referrer@example.com", password="pass"
        )
        code = ReferralCode.objects.create(user=referrer, code="PRORATA")
        return Referral.objects.create(
            referral_code=code,
            referred_user=fee_organization.owner,
            revenue_share_percent=Decimal("100.00"),
        )

    @pytest.mark.parametrize(
        ("refunded_guest", "retained", "billable_fee"),
        [
            # An even split billed 2.50 on 80.00 retained — 3.125% of a 5% deal.
            ("Galleria", Decimal("80.00"), Decimal("4.00")),
            # An even split billed 2.50 on 20.00 retained — 12.5%, the support ticket.
            ("Premium", Decimal("20.00"), Decimal("1.00")),
            (None, Decimal("100.00"), Decimal("5.00")),
        ],
        ids=["refund-cheap", "refund-expensive", "no-refund"],
    )
    def test_invoice_bills_the_fee_on_what_the_org_kept(
        self,
        cart: dict[str, Payment],
        fee_organization: Organization,
        site_settings: SiteSettings,
        refunded_guest: str | None,
        retained: Decimal,
        billable_fee: Decimal,
    ) -> None:
        """The org's invoice must charge exactly 5% of the revenue that survived."""
        if refunded_guest:
            _refund(cart[refunded_guest])

        invoice = _invoice_for(fee_organization)

        assert invoice.total_ticket_revenue == retained
        # fee_net is the fee proper; fee_gross carries the platform's VAT on top.
        assert invoice.fee_net == billable_fee
        assert invoice.fee_net / invoice.total_ticket_revenue == Decimal("0.05")
        # The VAT on the fee is weighted too, and still reconciles row by row.
        assert invoice.fee_vat > Decimal("0.00")
        assert invoice.fee_gross == invoice.fee_net + invoice.fee_vat

    @pytest.mark.parametrize(
        ("refunded_guest", "retained", "billable_fee"),
        [
            ("Galleria", Decimal("80.00"), Decimal("4.00")),
            ("Premium", Decimal("20.00"), Decimal("1.00")),
        ],
        ids=["refund-cheap", "refund-expensive"],
    )
    def test_referral_payout_follows_the_retained_revenue(
        self,
        cart: dict[str, Payment],
        referral: Referral,
        refunded_guest: str,
        retained: Decimal,
        billable_fee: Decimal,
    ) -> None:
        """The referrer is paid on the net fee of what the org actually kept.

        At a 100% revenue share the payout *is* the net fee, so an even split would
        have paid 2.50 in both directions instead of 4.00 and 1.00.
        """
        _refund(cart[refunded_guest])
        survivor = next(p for guest, p in cart.items() if guest != refunded_guest)
        survivor.refresh_from_db()
        today = timezone.localdate()

        calculate_payouts_for_period(today, today)

        payout = ReferralPayout.objects.get(referral=referral)
        assert survivor.amount == retained
        assert payout.net_platform_fees == billable_fee
        assert payout.payout_amount == billable_fee
