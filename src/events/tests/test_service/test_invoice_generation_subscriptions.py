"""Tests for membership subscription platform fees in monthly invoice generation.

Tests cover:
- Subscription-only orgs still get an invoice
- Ticket + subscription fees merge into a single invoice per org x currency
- Reverse charge / dominant VAT rate decided across both fee sources
- Non-succeeded membership payments are excluded
"""

import typing as t
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import (
    Event,
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
)
from events.models.organization import Organization
from events.models.ticket import Payment, Ticket, TicketTier
from events.service.invoice_service import generate_invoices_for_period

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def owner(django_user_model: type[RevelUser]) -> RevelUser:
    """Organization owner with a real email."""
    return django_user_model.objects.create_user(
        username="sub_inv_owner",
        email="sub_inv_owner@example.com",
        password="pass",
        email_verified=True,
    )


@pytest.fixture
def buyer(django_user_model: type[RevelUser]) -> RevelUser:
    """User who purchases tickets."""
    return django_user_model.objects.create_user(
        username="sub_inv_buyer",
        email="sub_inv_buyer@example.com",
        password="pass",
    )


@pytest.fixture
def org(owner: RevelUser) -> Organization:
    """Organization with VAT / billing fields populated."""
    return Organization.objects.create(
        name="Sub Invoice Org",
        slug="sub-invoice-org",
        owner=owner,
        vat_id="DE123456789",
        vat_country_code="DE",
        billing_address="Musterstr. 1, 10115 Berlin",
        billing_email="billing@sub.com",
    )


@pytest.fixture
def sub_event(org: Organization) -> Event:
    """Event belonging to the org fixture."""
    now = timezone.now()
    return Event.objects.create(
        organization=org,
        name="Sub Invoice Event",
        slug="sub-invoice-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        max_attendees=200,
        start=now,
        end=now + timedelta(hours=4),
        status=Event.EventStatus.OPEN,
        requires_ticket=True,
    )


@pytest.fixture
def ticket_tier(sub_event: Event) -> TicketTier:
    """A paid ticket tier linked to the event."""
    return TicketTier.objects.create(
        event=sub_event,
        name="Standard",
        price=Decimal("25.00"),
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )


@pytest.fixture
def plan(org: Organization) -> MembershipSubscriptionPlan:
    """An online-billed monthly membership plan."""
    tier = MembershipTier.objects.create(organization=org, name="Members")
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly",
        price=Decimal("10.00"),
        currency="EUR",
        payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
    )


@pytest.fixture
def make_subscription(
    django_user_model: type[RevelUser],
    org: Organization,
    plan: MembershipSubscriptionPlan,
) -> t.Callable[[], MembershipSubscription]:
    """Factory for active subscriptions (one per user, per the org constraint)."""
    counter = {"n": 0}

    def _make() -> MembershipSubscription:
        counter["n"] += 1
        user = django_user_model.objects.create_user(
            username=f"sub_inv_member_{counter['n']}",
            email=f"sub_inv_member_{counter['n']}@example.com",
            password="pass",
        )
        return MembershipSubscription.objects.create(
            user=user,
            plan=plan,
            organization=org,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )

    return _make


@pytest.fixture
def site_settings() -> SiteSettings:
    """Populate SiteSettings singleton with platform business details."""
    site = SiteSettings.get_solo()
    site.platform_business_name = "Revel S.r.l."
    site.platform_business_address = "Via Roma 1, 00100 Roma, Italy"
    site.platform_vat_id = "IT12345678901"
    site.platform_vat_rate = Decimal("22.00")
    site.save()
    return site


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_ticket_payment(
    *,
    event: Event,
    tier: TicketTier,
    user: RevelUser,
    suffix: str,
    created_at: datetime,
    amount: Decimal = Decimal("25.00"),
    platform_fee: Decimal = Decimal("2.50"),
    platform_fee_net: Decimal = Decimal("2.05"),
    platform_fee_vat: Decimal = Decimal("0.45"),
    platform_fee_vat_rate: Decimal | None = Decimal("22.00"),
    platform_fee_reverse_charge: bool = False,
    currency: str = "EUR",
) -> Payment:
    """Create a succeeded ticket Payment inside the billing window."""
    ticket = Ticket.objects.create(event=event, user=user, tier=tier, guest_name=f"Guest{suffix}")
    payment = Payment.objects.create(
        ticket=ticket,
        user=user,
        stripe_session_id=f"cs_test_{ticket.pk}",
        status=Payment.PaymentStatus.SUCCEEDED,
        amount=amount,
        platform_fee=platform_fee,
        platform_fee_net=platform_fee_net,
        platform_fee_vat=platform_fee_vat,
        platform_fee_vat_rate=platform_fee_vat_rate,
        platform_fee_reverse_charge=platform_fee_reverse_charge,
        currency=currency,
    )
    Payment.objects.filter(pk=payment.pk).update(created_at=created_at)
    return payment


def _create_membership_payment(
    *,
    subscription: MembershipSubscription,
    created_at: datetime,
    amount: Decimal = Decimal("10.00"),
    platform_fee: Decimal = Decimal("1.00"),
    platform_fee_net: Decimal | None = None,
    platform_fee_vat: Decimal | None = None,
    platform_fee_vat_rate: Decimal | None = None,
    platform_fee_reverse_charge: bool = False,
    currency: str = "EUR",
    status: str = MembershipPayment.PaymentStatus.SUCCEEDED,
) -> MembershipPayment:
    """Create a MembershipPayment with a controlled ``created_at``."""
    payment = MembershipPayment.objects.create(
        subscription=subscription,
        amount=amount,
        currency=currency,
        status=status,
        period_start=created_at,
        period_end=created_at + timedelta(days=30),
        platform_fee=platform_fee,
        platform_fee_net=platform_fee_net if platform_fee_net is not None else platform_fee,
        platform_fee_vat=platform_fee_vat if platform_fee_vat is not None else Decimal("0.00"),
        platform_fee_vat_rate=platform_fee_vat_rate,
        platform_fee_reverse_charge=platform_fee_reverse_charge,
    )
    # Bypass auto_now_add by using queryset update
    MembershipPayment.objects.filter(pk=payment.pk).update(created_at=created_at)
    payment.refresh_from_db()
    return payment


# ===========================================================================
# generate_invoices_for_period — subscription fees
# ===========================================================================


@patch("common.service.invoice_utils.HTML")
class TestSubscriptionPlatformFeeInvoicing:
    """Membership subscription fees are billed alongside ticket fees."""

    def test_subscription_only_org_gets_invoice(
        self,
        mock_html_cls: MagicMock,
        org: Organization,
        make_subscription: t.Callable[[], MembershipSubscription],
        site_settings: SiteSettings,
    ) -> None:
        """An org whose only platform fees come from subscriptions is still invoiced."""
        mock_html_cls.return_value.write_pdf.return_value = None
        _create_membership_payment(
            subscription=make_subscription(),
            created_at=timezone.make_aware(datetime(2027, 1, 15, 12, 0)),
            amount=Decimal("10.00"),
            platform_fee=Decimal("3.00"),
            platform_fee_net=Decimal("2.46"),
            platform_fee_vat=Decimal("0.54"),
            platform_fee_vat_rate=Decimal("22.00"),
        )

        invoices = generate_invoices_for_period(date(2027, 1, 1), date(2027, 1, 31))

        assert len(invoices) == 1
        inv = invoices[0]
        assert inv.organization == org
        assert inv.fee_gross == Decimal("3.00")
        assert inv.fee_net == Decimal("2.46")
        assert inv.fee_vat == Decimal("0.54")
        assert inv.fee_vat_rate == Decimal("22.00")
        assert inv.reverse_charge is False
        assert inv.total_subscription_payments == 1
        assert inv.total_subscription_revenue == Decimal("10.00")
        assert inv.total_tickets == 0
        assert inv.total_ticket_revenue == Decimal("0.00")

    def test_ticket_and_subscription_fees_merge_into_one_invoice(
        self,
        mock_html_cls: MagicMock,
        org: Organization,
        sub_event: Event,
        ticket_tier: TicketTier,
        buyer: RevelUser,
        make_subscription: t.Callable[[], MembershipSubscription],
        site_settings: SiteSettings,
    ) -> None:
        """Both fee sources in the same currency yield a single summed invoice."""
        mock_html_cls.return_value.write_pdf.return_value = None
        created_at = timezone.make_aware(datetime(2027, 2, 10, 12, 0))
        _create_ticket_payment(
            event=sub_event,
            tier=ticket_tier,
            user=buyer,
            suffix="_mix",
            created_at=created_at,
        )
        _create_membership_payment(
            subscription=make_subscription(),
            created_at=created_at,
            amount=Decimal("10.00"),
            platform_fee=Decimal("1.00"),
            platform_fee_net=Decimal("0.82"),
            platform_fee_vat=Decimal("0.18"),
            platform_fee_vat_rate=Decimal("22.00"),
        )

        invoices = generate_invoices_for_period(date(2027, 2, 1), date(2027, 2, 28))

        assert len(invoices) == 1
        inv = invoices[0]
        assert inv.fee_gross == Decimal("3.50")
        assert inv.fee_net == Decimal("2.87")
        assert inv.fee_vat == Decimal("0.63")
        # Ticket stats stay ticket-only; subscription stats carry the other side.
        assert inv.total_tickets == 1
        assert inv.total_ticket_revenue == Decimal("25.00")
        assert inv.total_subscription_payments == 1
        assert inv.total_subscription_revenue == Decimal("10.00")

    def test_all_reverse_charge_across_both_sources(
        self,
        mock_html_cls: MagicMock,
        org: Organization,
        sub_event: Event,
        ticket_tier: TicketTier,
        buyer: RevelUser,
        make_subscription: t.Callable[[], MembershipSubscription],
        site_settings: SiteSettings,
    ) -> None:
        """Reverse charge applies only when every payment of both sources uses it."""
        mock_html_cls.return_value.write_pdf.return_value = None
        created_at = timezone.make_aware(datetime(2027, 3, 10, 12, 0))
        _create_ticket_payment(
            event=sub_event,
            tier=ticket_tier,
            user=buyer,
            suffix="_rc",
            created_at=created_at,
            platform_fee=Decimal("2.50"),
            platform_fee_net=Decimal("2.50"),
            platform_fee_vat=Decimal("0.00"),
            platform_fee_vat_rate=Decimal("0.00"),
            platform_fee_reverse_charge=True,
        )
        _create_membership_payment(
            subscription=make_subscription(),
            created_at=created_at,
            platform_fee=Decimal("1.00"),
            platform_fee_vat_rate=Decimal("0.00"),
            platform_fee_reverse_charge=True,
        )

        invoices = generate_invoices_for_period(date(2027, 3, 1), date(2027, 3, 31))

        assert len(invoices) == 1
        assert invoices[0].reverse_charge is True
        assert invoices[0].fee_vat_rate == Decimal("0.00")

    def test_reverse_charge_ticket_with_normal_subscription_is_not_reverse_charge(
        self,
        mock_html_cls: MagicMock,
        org: Organization,
        sub_event: Event,
        ticket_tier: TicketTier,
        buyer: RevelUser,
        make_subscription: t.Callable[[], MembershipSubscription],
        site_settings: SiteSettings,
    ) -> None:
        """A non-RC subscription payment breaks reverse charge for the whole invoice."""
        mock_html_cls.return_value.write_pdf.return_value = None
        created_at = timezone.make_aware(datetime(2027, 4, 10, 12, 0))
        _create_ticket_payment(
            event=sub_event,
            tier=ticket_tier,
            user=buyer,
            suffix="_partial_rc",
            created_at=created_at,
            platform_fee_net=Decimal("2.50"),
            platform_fee_vat=Decimal("0.00"),
            platform_fee_vat_rate=Decimal("0.00"),
            platform_fee_reverse_charge=True,
        )
        _create_membership_payment(
            subscription=make_subscription(),
            created_at=created_at,
            platform_fee_vat_rate=Decimal("19.00"),
        )

        invoices = generate_invoices_for_period(date(2027, 4, 1), date(2027, 4, 30))

        assert len(invoices) == 1
        assert invoices[0].reverse_charge is False
        # Only the non-RC subscription payment carries a usable rate.
        assert invoices[0].fee_vat_rate == Decimal("19.00")

    def test_dominant_vat_rate_counts_both_sources(
        self,
        mock_html_cls: MagicMock,
        org: Organization,
        sub_event: Event,
        ticket_tier: TicketTier,
        buyer: RevelUser,
        make_subscription: t.Callable[[], MembershipSubscription],
        site_settings: SiteSettings,
    ) -> None:
        """The dominant rate is the most common one pooled across both sources."""
        mock_html_cls.return_value.write_pdf.return_value = None
        created_at = timezone.make_aware(datetime(2027, 5, 10, 12, 0))
        _create_ticket_payment(
            event=sub_event,
            tier=ticket_tier,
            user=buyer,
            suffix="_dom",
            created_at=created_at,
            platform_fee_vat_rate=Decimal("19.00"),
        )
        subscription = make_subscription()
        for _ in range(2):
            _create_membership_payment(
                subscription=subscription,
                created_at=created_at,
                platform_fee_vat_rate=Decimal("22.00"),
            )

        invoices = generate_invoices_for_period(date(2027, 5, 1), date(2027, 5, 31))

        assert len(invoices) == 1
        assert invoices[0].fee_vat_rate == Decimal("22.00")
        assert invoices[0].reverse_charge is False
        assert invoices[0].total_subscription_payments == 2

    def test_offline_zero_fee_payments_do_not_break_reverse_charge(
        self,
        mock_html_cls: MagicMock,
        org: Organization,
        make_subscription: t.Callable[[], MembershipSubscription],
        site_settings: SiteSettings,
    ) -> None:
        """Zero-fee (OFFLINE/staff-recorded) rows carry no VAT decision."""
        mock_html_cls.return_value.write_pdf.return_value = None
        created_at = timezone.make_aware(datetime(2027, 6, 10, 12, 0))
        _create_membership_payment(
            subscription=make_subscription(),
            created_at=created_at,
            platform_fee=Decimal("2.00"),
            platform_fee_vat_rate=Decimal("0.00"),
            platform_fee_reverse_charge=True,
        )
        _create_membership_payment(
            subscription=make_subscription(),
            created_at=created_at,
            platform_fee=Decimal("0.00"),
        )

        invoices = generate_invoices_for_period(date(2027, 6, 1), date(2027, 6, 30))

        assert len(invoices) == 1
        assert invoices[0].reverse_charge is True
        assert invoices[0].fee_gross == Decimal("2.00")
        assert invoices[0].total_subscription_payments == 2

    def test_non_succeeded_membership_payments_excluded(
        self,
        mock_html_cls: MagicMock,
        org: Organization,
        make_subscription: t.Callable[[], MembershipSubscription],
        site_settings: SiteSettings,
    ) -> None:
        """Refunded / failed / pending membership payments produce no invoice."""
        mock_html_cls.return_value.write_pdf.return_value = None
        created_at = timezone.make_aware(datetime(2027, 7, 10, 12, 0))
        for status in (
            MembershipPayment.PaymentStatus.REFUNDED,
            MembershipPayment.PaymentStatus.FAILED,
            MembershipPayment.PaymentStatus.PENDING,
        ):
            _create_membership_payment(
                subscription=make_subscription(),
                created_at=created_at,
                platform_fee=Decimal("5.00"),
                status=status,
            )

        assert generate_invoices_for_period(date(2027, 7, 1), date(2027, 7, 31)) == []

    def test_subscription_payments_outside_period_excluded(
        self,
        mock_html_cls: MagicMock,
        org: Organization,
        make_subscription: t.Callable[[], MembershipSubscription],
        site_settings: SiteSettings,
    ) -> None:
        """Membership payments outside the billing window are ignored."""
        mock_html_cls.return_value.write_pdf.return_value = None
        _create_membership_payment(
            subscription=make_subscription(),
            created_at=timezone.make_aware(datetime(2027, 7, 31, 23, 59)),
            platform_fee=Decimal("5.00"),
        )
        _create_membership_payment(
            subscription=make_subscription(),
            created_at=timezone.make_aware(datetime(2027, 9, 1, 0, 1)),
            platform_fee=Decimal("5.00"),
        )

        assert generate_invoices_for_period(date(2027, 8, 1), date(2027, 8, 31)) == []

    def test_separate_invoices_per_currency(
        self,
        mock_html_cls: MagicMock,
        org: Organization,
        make_subscription: t.Callable[[], MembershipSubscription],
        site_settings: SiteSettings,
    ) -> None:
        """Subscription fees in different currencies stay on separate invoices."""
        mock_html_cls.return_value.write_pdf.return_value = None
        created_at = timezone.make_aware(datetime(2027, 10, 10, 12, 0))
        _create_membership_payment(
            subscription=make_subscription(),
            created_at=created_at,
            platform_fee=Decimal("1.00"),
            currency="EUR",
        )
        _create_membership_payment(
            subscription=make_subscription(),
            created_at=created_at,
            platform_fee=Decimal("2.00"),
            currency="USD",
        )

        invoices = generate_invoices_for_period(date(2027, 10, 1), date(2027, 10, 31))

        assert {inv.currency: inv.fee_gross for inv in invoices} == {
            "EUR": Decimal("1.00"),
            "USD": Decimal("2.00"),
        }
