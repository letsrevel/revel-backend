"""Tests for subscription platform-fee collection and its VAT ledger.

Covers the two halves of the fee flow:
- ``_effective_application_fee_percent``: the VAT-grossed percent sent to
  Stripe at Checkout-session creation.
- ``record_stripe_payment_from_invoice``: decomposing the collected fee — read
  off the invoice's PaymentIntent at the pinned dahlia API version — back into
  net + VAT on the ledger row.
"""

from decimal import Decimal
from unittest import mock

import pytest
import stripe

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service import subscription_stripe_service, subscription_stripe_sync

pytestmark = pytest.mark.django_db


@pytest.fixture
def site_settings() -> SiteSettings:
    """Platform registered in Austria with a 20% domestic VAT rate."""
    site = SiteSettings.get_solo()
    site.platform_vat_country = "AT"
    site.platform_vat_rate = Decimal("20.00")
    site.save()
    return site


@pytest.fixture
def stripe_org(organization: Organization) -> Organization:
    """A Stripe-connected Austrian org charging the default 1.50% fee."""
    organization.stripe_account_id = "acct_test_org"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.platform_fee_percent = Decimal("1.50")
    organization.vat_country_code = "AT"
    organization.save()
    return organization


@pytest.fixture
def online_plan(stripe_org: Organization) -> MembershipSubscriptionPlan:
    """An ONLINE plan with pre-populated Stripe IDs (skips ensure_stripe_price)."""
    tier = MembershipTier.objects.get(organization=stripe_org, name="General membership")
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly Online",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit="month",
        period_count=1,
        payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        stripe_product_id="prod_test",
        stripe_price_id="price_test",
    )


@pytest.fixture
def subscriber(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="fee_subscriber", email="fee@example.com", password="pass")


def _reverse_charge(org: Organization) -> None:
    """Turn the org into an EU cross-border entity with a validated VAT ID."""
    org.vat_country_code = "DE"
    org.vat_id = "DE123456789"
    org.vat_id_validated = True
    org.save()


# ---- application_fee_percent at Checkout ------------------------------------


class TestEffectiveApplicationFeePercent:
    @mock.patch("events.service.subscription_stripe_service.stripe.checkout.Session.create")
    @mock.patch("events.service.subscription_stripe_service.stripe.Customer.create")
    def test_domestic_org_percent_is_grossed_up_with_vat(
        self,
        mock_customer: mock.Mock,
        mock_session: mock.Mock,
        site_settings: SiteSettings,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """1.50% + 20% VAT on the fee = 1.80% collected via Stripe."""
        mock_customer.return_value = mock.MagicMock(id="cus_fee")
        mock_session.return_value = mock.MagicMock(id="cs_fee", url="https://checkout.stripe.com/c/pay/cs_fee")

        subscription_stripe_service.start_online_subscription(online_plan, subscriber)

        subscription_data = mock_session.call_args.kwargs["subscription_data"]
        assert subscription_data["application_fee_percent"] == 1.80

    @mock.patch("events.service.subscription_stripe_service.stripe.checkout.Session.create")
    @mock.patch("events.service.subscription_stripe_service.stripe.Customer.create")
    def test_reverse_charge_org_percent_is_untouched(
        self,
        mock_customer: mock.Mock,
        mock_session: mock.Mock,
        site_settings: SiteSettings,
        stripe_org: Organization,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """Reverse charge: the org self-assesses, so no gross-up."""
        _reverse_charge(stripe_org)
        mock_customer.return_value = mock.MagicMock(id="cus_rc")
        mock_session.return_value = mock.MagicMock(id="cs_rc", url="https://checkout.stripe.com/c/pay/cs_rc")

        subscription_stripe_service.start_online_subscription(online_plan, subscriber)

        subscription_data = mock_session.call_args.kwargs["subscription_data"]
        assert subscription_data["application_fee_percent"] == 1.50

    @mock.patch("events.service.subscription_stripe_service.stripe.checkout.Session.create")
    @mock.patch("events.service.subscription_stripe_service.stripe.Customer.create")
    def test_zero_percent_org_sends_no_application_fee(
        self,
        mock_customer: mock.Mock,
        mock_session: mock.Mock,
        site_settings: SiteSettings,
        stripe_org: Organization,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> None:
        """A fee-free org must not get a 0% application fee key on the payload."""
        stripe_org.platform_fee_percent = Decimal("0.00")
        stripe_org.save(update_fields=["platform_fee_percent"])
        mock_customer.return_value = mock.MagicMock(id="cus_free")
        mock_session.return_value = mock.MagicMock(id="cs_free", url="https://checkout.stripe.com/c/pay/cs_free")

        subscription_stripe_service.start_online_subscription(online_plan, subscriber)

        subscription_data = mock_session.call_args.kwargs["subscription_data"]
        assert "application_fee_percent" not in subscription_data

    def test_percent_is_capped_at_100(self, site_settings: SiteSettings, stripe_org: Organization) -> None:
        """Stripe rejects >100; a 100% org grossed up by 20% must clamp."""
        stripe_org.platform_fee_percent = Decimal("100.00")

        assert subscription_stripe_service.effective_application_fee_percent(stripe_org) == Decimal("100")


# ---- fee ledger on MembershipPayment ----------------------------------------


class TestRecordedPlatformFee:
    """The pinned dahlia API version removed the Invoice's readable
    ``application_fee_amount``: the collected fee is only readable off the
    invoice's PaymentIntent (``payments.data[].payment.payment_intent``), which
    webhook payloads don't embed — so the fee rides the same outbound
    ``Invoice.retrieve`` fallback that resolves the intent id. These payloads
    are dahlia-shaped on purpose: a legacy-shaped fixture would mask a
    regression where every ONLINE payment records a zero fee.
    """

    RETRIEVE = "events.service.subscription_stripe_payloads.stripe.Invoice.retrieve"

    @pytest.fixture
    def subscription(
        self,
        online_plan: MembershipSubscriptionPlan,
        subscriber: RevelUser,
    ) -> MembershipSubscription:
        return MembershipSubscription.objects.create(
            user=subscriber,
            plan=online_plan,
            organization=online_plan.tier.organization,
            status=MembershipSubscription.SubscriptionStatus.PENDING,
            stripe_subscription_id="sub_fee",
        )

    @staticmethod
    def _invoice(invoice_id: str, **extra: object) -> dict[str, object]:
        """A dahlia-shaped invoice: no readable ``application_fee_amount`` or
        top-level ``payment_intent``/``subscription``, and no embedded ``payments``."""
        return {
            "id": invoice_id,
            "parent": {"subscription_details": {"subscription": "sub_fee"}},
            "amount_paid": 1000,
            "currency": "eur",
            "lines": {"data": [{"period": {"start": 1_800_000_000, "end": 1_800_000_000 + 30 * 86400}}]},
            **extra,
        }

    @staticmethod
    def _retrieved(fee_minor: int | None) -> dict[str, object]:
        """What the deep-expanded ``Invoice.retrieve`` fallback returns."""
        return {
            "payments": {
                "data": [
                    {
                        "payment": {
                            "type": "payment_intent",
                            "payment_intent": {"id": "pi_fee", "application_fee_amount": fee_minor},
                        }
                    }
                ]
            }
        }

    def test_domestic_fee_is_decomposed_into_net_and_vat(
        self,
        site_settings: SiteSettings,
        subscription: MembershipSubscription,
    ) -> None:
        """Dahlia ``invoice.paid``: 1.80 EUR off the PaymentIntent = 1.50 net + 0.30 VAT."""
        with mock.patch(self.RETRIEVE, return_value=self._retrieved(180)) as mock_retrieve:
            payment = subscription_stripe_sync.record_stripe_payment_from_invoice(
                self._invoice("in_fee"), succeeded=True
            )

        assert payment is not None
        assert payment.stripe_payment_intent_id == "pi_fee"
        assert payment.platform_fee == Decimal("1.80")
        assert payment.platform_fee_net == Decimal("1.50")
        assert payment.platform_fee_vat == Decimal("0.30")
        assert payment.platform_fee_vat_rate == Decimal("20.00")
        assert payment.platform_fee_reverse_charge is False
        # Direct-charge Connect call, expanded down to the PaymentIntent.
        assert mock_retrieve.call_args.kwargs.get("expand") == ["payments.data.payment.payment_intent"]
        assert mock_retrieve.call_args.kwargs.get("stripe_account") == "acct_test_org"

    def test_reverse_charge_fee_is_all_net(
        self,
        site_settings: SiteSettings,
        stripe_org: Organization,
        subscription: MembershipSubscription,
    ) -> None:
        """Reverse charge: no VAT was collected, so the whole fee is net."""
        _reverse_charge(stripe_org)
        with mock.patch(self.RETRIEVE, return_value=self._retrieved(150)):
            payment = subscription_stripe_sync.record_stripe_payment_from_invoice(
                self._invoice("in_fee_rc"), succeeded=True
            )

        assert payment is not None
        assert payment.platform_fee == Decimal("1.50")
        assert payment.platform_fee_net == Decimal("1.50")
        assert payment.platform_fee_vat == Decimal("0.00")
        assert payment.platform_fee_reverse_charge is True

    def test_invoice_without_application_fee_records_zero(
        self,
        site_settings: SiteSettings,
        subscription: MembershipSubscription,
    ) -> None:
        """A fee-free org's PaymentIntent carries no fee — VAT breakdown stays null."""
        with mock.patch(self.RETRIEVE, return_value=self._retrieved(None)):
            payment = subscription_stripe_sync.record_stripe_payment_from_invoice(
                self._invoice("in_no_fee"), succeeded=True
            )

        assert payment is not None
        assert payment.platform_fee == Decimal("0.00")
        assert payment.platform_fee_net is None
        assert payment.platform_fee_vat is None
        assert payment.platform_fee_vat_rate is None
        assert payment.platform_fee_reverse_charge is False

    def test_failed_invoice_records_no_fee(
        self,
        site_settings: SiteSettings,
        subscription: MembershipSubscription,
    ) -> None:
        """Nothing changed hands on a failed invoice, so no fee was collected."""
        invoice = self._invoice("in_fee_failed", amount_paid=0, amount_due=1000)

        with mock.patch(self.RETRIEVE, return_value=self._retrieved(180)):
            payment = subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=False)

        assert payment is not None
        assert payment.platform_fee == Decimal("0.00")
        assert payment.platform_fee_net is None

    def test_legacy_invoice_fee_field_is_still_honored(
        self,
        site_settings: SiteSettings,
        subscription: MembershipSubscription,
    ) -> None:
        """A pre-basil payload's readable fee field wins — no outbound fetch."""
        invoice = self._invoice("in_fee_legacy", payment_intent="pi_legacy", application_fee_amount=180)

        with mock.patch(self.RETRIEVE, side_effect=AssertionError("must not fetch")):
            payment = subscription_stripe_sync.record_stripe_payment_from_invoice(invoice, succeeded=True)

        assert payment is not None
        assert payment.stripe_payment_intent_id == "pi_legacy"
        assert payment.platform_fee == Decimal("1.80")
        assert payment.platform_fee_net == Decimal("1.50")

    def test_fee_fetch_failure_records_payment_with_zero_fee(
        self,
        site_settings: SiteSettings,
        subscription: MembershipSubscription,
    ) -> None:
        """Stripe unreachable: the payment row must land anyway, fee at zero."""
        with mock.patch(self.RETRIEVE, side_effect=stripe.error.StripeError("boom")):
            payment = subscription_stripe_sync.record_stripe_payment_from_invoice(
                self._invoice("in_fee_down"), succeeded=True
            )

        assert payment is not None
        assert payment.status == MembershipPayment.PaymentStatus.SUCCEEDED
        assert payment.platform_fee == Decimal("0.00")
        assert payment.platform_fee_net is None
