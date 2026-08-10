"""Tests for the application_fee_percent resync on VAT-status changes.

Three layers:
- ``resync_subscription_application_fees``: which subscriptions get the new
  percent pushed, which are skipped, and how Stripe failures are contained.
- The :mod:`events.service.vies_service` wrappers: a VAT mutation dispatches
  the resync task only when the *effective* percent actually moved.
- The ``events.resync_org_subscription_fees`` Celery task glue.
"""

import typing as t
from decimal import Decimal
from unittest import mock

import pytest
import stripe
from ninja.errors import HttpError

from accounts.models import RevelUser
from common.models import SiteSettings
from common.service.vies_service import VIESValidationResult
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)
from events.service import subscription_stripe_service, vies_service
from events.tasks import resync_org_subscription_fees

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


def _make_sub(
    plan: MembershipSubscriptionPlan,
    username: str,
    *,
    stripe_subscription_id: str | None = None,
    status: str = MembershipSubscription.SubscriptionStatus.ACTIVE,
    stripe_schedule_id: str = "",
) -> MembershipSubscription:
    user = RevelUser.objects.create_user(username=username, email=f"{username}@example.com", password="pass")
    return MembershipSubscription.objects.create(
        user=user,
        plan=plan,
        organization=plan.tier.organization,
        status=status,
        stripe_subscription_id=stripe_subscription_id,
        stripe_schedule_id=stripe_schedule_id,
    )


# ---- resync_subscription_application_fees -----------------------------------


class TestResyncService:
    @mock.patch("events.service.subscription_stripe_service.stripe.Subscription.modify")
    def test_pushes_grossed_percent_to_live_subscriptions(
        self,
        mock_modify: mock.Mock,
        site_settings: SiteSettings,
        stripe_org: Organization,
        online_plan: MembershipSubscriptionPlan,
    ) -> None:
        """Domestic org: 1.50% + 20% VAT = 1.80% pushed on the Connect account."""
        _make_sub(online_plan, "resync_a", stripe_subscription_id="sub_a")
        _make_sub(online_plan, "resync_b", stripe_subscription_id="sub_b")

        counters = subscription_stripe_service.resync_subscription_application_fees(stripe_org)

        assert counters == {"updated": 2, "skipped_schedule_managed": 0, "failed": 0}
        assert mock_modify.call_count == 2
        called_ids = {call.args[0] for call in mock_modify.call_args_list}
        assert called_ids == {"sub_a", "sub_b"}
        for call in mock_modify.call_args_list:
            assert call.kwargs["application_fee_percent"] == 1.80
            assert call.kwargs["stripe_account"] == "acct_test_org"

    @mock.patch("events.service.subscription_stripe_service.stripe.Subscription.modify")
    def test_fee_free_org_clears_the_percent(
        self,
        mock_modify: mock.Mock,
        site_settings: SiteSettings,
        stripe_org: Organization,
        online_plan: MembershipSubscriptionPlan,
    ) -> None:
        """No effective fee sends Stripe's unset sentinel, mirroring pause_collection=''."""
        stripe_org.platform_fee_percent = Decimal("0.00")
        stripe_org.save(update_fields=["platform_fee_percent"])
        _make_sub(online_plan, "resync_free", stripe_subscription_id="sub_free")

        counters = subscription_stripe_service.resync_subscription_application_fees(stripe_org)

        assert counters["updated"] == 1
        assert mock_modify.call_args.kwargs["application_fee_percent"] == ""

    @mock.patch("events.service.subscription_stripe_service.stripe.Subscription.modify")
    def test_schedule_managed_subscription_is_skipped(
        self,
        mock_modify: mock.Mock,
        site_settings: SiteSettings,
        stripe_org: Organization,
        online_plan: MembershipSubscriptionPlan,
    ) -> None:
        """A pending downgrade must not be released/clobbered — skip and count."""
        _make_sub(online_plan, "resync_sched", stripe_subscription_id="sub_sched", stripe_schedule_id="sub_sched_1")
        _make_sub(online_plan, "resync_plain", stripe_subscription_id="sub_plain")

        counters = subscription_stripe_service.resync_subscription_application_fees(stripe_org)

        assert counters == {"updated": 1, "skipped_schedule_managed": 1, "failed": 0}
        assert mock_modify.call_count == 1
        assert mock_modify.call_args.args[0] == "sub_plain"

    @mock.patch("events.service.subscription_stripe_service.stripe.Subscription.modify")
    def test_terminal_offline_and_unlinked_rows_are_excluded(
        self,
        mock_modify: mock.Mock,
        site_settings: SiteSettings,
        stripe_org: Organization,
        online_plan: MembershipSubscriptionPlan,
    ) -> None:
        _make_sub(
            online_plan,
            "resync_cancelled",
            stripe_subscription_id="sub_dead",
            status=MembershipSubscription.SubscriptionStatus.CANCELLED,
        )
        _make_sub(online_plan, "resync_unlinked")  # PENDING checkout, no Stripe sub yet
        offline_plan = MembershipSubscriptionPlan.objects.create(
            tier=online_plan.tier,
            name="Offline",
            price=Decimal("10.00"),
            currency="EUR",
            period_unit="month",
            period_count=1,
            payment_method=MembershipSubscriptionPlan.PaymentMethod.OFFLINE,
        )
        _make_sub(offline_plan, "resync_offline", stripe_subscription_id="sub_off")

        counters = subscription_stripe_service.resync_subscription_application_fees(stripe_org)

        assert counters == {"updated": 0, "skipped_schedule_managed": 0, "failed": 0}
        mock_modify.assert_not_called()

    @mock.patch("events.service.subscription_stripe_service.stripe.Subscription.modify")
    def test_null_subscription_id_does_not_strand_live_rows(
        self,
        mock_modify: mock.Mock,
        site_settings: SiteSettings,
        stripe_org: Organization,
        online_plan: MembershipSubscriptionPlan,
    ) -> None:
        """A NULL (mid-checkout/revival) row must be excluded, not passed to Stripe.

        ``exclude(stripe_subscription_id="")`` alone keeps NULL rows, and the
        ``-created_at`` ordering puts the freshest one first — a ``modify(None)``
        TypeError would escape the StripeError handler and kill the whole resync.
        """
        _make_sub(online_plan, "resync_linked", stripe_subscription_id="sub_linked")
        _make_sub(online_plan, "resync_pending", status=MembershipSubscription.SubscriptionStatus.PENDING)

        counters = subscription_stripe_service.resync_subscription_application_fees(stripe_org)

        assert counters == {"updated": 1, "skipped_schedule_managed": 0, "failed": 0}
        assert mock_modify.call_count == 1
        assert mock_modify.call_args.args[0] == "sub_linked"

    @mock.patch("events.service.subscription_stripe_service.stripe.Subscription.modify")
    def test_stripe_failure_is_counted_and_does_not_strand_the_rest(
        self,
        mock_modify: mock.Mock,
        site_settings: SiteSettings,
        stripe_org: Organization,
        online_plan: MembershipSubscriptionPlan,
    ) -> None:
        _make_sub(online_plan, "resync_fail", stripe_subscription_id="sub_fail")
        _make_sub(online_plan, "resync_ok", stripe_subscription_id="sub_ok")

        def _modify(sub_id: str, **kwargs: t.Any) -> mock.Mock:
            if sub_id == "sub_fail":
                raise stripe.error.InvalidRequestError("nope", param=None)
            return mock.MagicMock()

        mock_modify.side_effect = _modify

        counters = subscription_stripe_service.resync_subscription_application_fees(stripe_org)

        assert counters == {"updated": 1, "skipped_schedule_managed": 0, "failed": 1}
        assert mock_modify.call_count == 2


# ---- VIES wrapper dispatch ---------------------------------------------------


def _vies_result(valid: bool) -> VIESValidationResult:
    return VIESValidationResult(valid=valid, name="ACME GmbH", address="Somewhere 1", request_identifier="req-1")


class TestVatChangeDispatch:
    @pytest.fixture
    def cross_border_org(self, site_settings: SiteSettings, stripe_org: Organization) -> Organization:
        """German org with an unvalidated VAT ID: currently grossed (1.80%)."""
        stripe_org.vat_country_code = "DE"
        stripe_org.vat_id = "DE123456789"
        stripe_org.vat_id_validated = False
        stripe_org.save()
        return stripe_org

    @mock.patch("events.tasks.subscriptions.resync_org_subscription_fees.delay")
    @mock.patch("common.service.vies_service.validate_vat_id")
    def test_revalidation_flip_dispatches_resync(
        self,
        mock_validate: mock.Mock,
        mock_delay: mock.Mock,
        cross_border_org: Organization,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Unvalidated→validated cross-border flips 1.80% → 1.50%: resync queued."""
        mock_validate.return_value = _vies_result(valid=True)

        with django_capture_on_commit_callbacks(execute=True):
            vies_service.validate_and_update_organization(cross_border_org)

        mock_delay.assert_called_once_with(str(cross_border_org.id))

    @mock.patch("events.tasks.subscriptions.resync_org_subscription_fees.delay")
    @mock.patch("common.service.vies_service.validate_vat_id")
    def test_revalidation_confirming_status_does_not_dispatch(
        self,
        mock_validate: mock.Mock,
        mock_delay: mock.Mock,
        cross_border_org: Organization,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """The monthly sweep's common case — nothing changed — must stay quiet."""
        cross_border_org.vat_id_validated = True
        cross_border_org.save(update_fields=["vat_id_validated"])
        mock_validate.return_value = _vies_result(valid=True)

        with django_capture_on_commit_callbacks(execute=True):
            vies_service.validate_and_update_organization(cross_border_org)

        mock_delay.assert_not_called()

    @mock.patch("events.tasks.subscriptions.resync_org_subscription_fees.delay")
    @mock.patch("common.service.vies_service.validate_vat_id")
    def test_invalid_vat_id_still_dispatches_after_the_400(
        self,
        mock_validate: mock.Mock,
        mock_delay: mock.Mock,
        cross_border_org: Organization,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """set_org_vat_id saves validated=False before raising; the state change is real."""
        cross_border_org.vat_id_validated = True
        cross_border_org.save(update_fields=["vat_id_validated"])
        mock_validate.return_value = _vies_result(valid=False)

        with django_capture_on_commit_callbacks(execute=True):
            with pytest.raises(HttpError):
                vies_service.set_org_vat_id(cross_border_org, "DE123456789")

        mock_delay.assert_called_once_with(str(cross_border_org.id))

    @mock.patch("events.tasks.subscriptions.resync_org_subscription_fees.delay")
    def test_clearing_vat_fields_dispatches_resync(
        self,
        mock_delay: mock.Mock,
        site_settings: SiteSettings,
        stripe_org: Organization,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Cross-border DE org (1.50% bare, reverse charge) → no country at all: resync queued.

        Clearing empties the country code, which the fee paths now treat as
        domestic (unknown-country fail-safe), so the percent flips from bare
        to grossed.
        """
        stripe_org.vat_country_code = "DE"
        stripe_org.vat_id = "DE123456789"
        stripe_org.vat_id_validated = True
        stripe_org.save(update_fields=["vat_country_code", "vat_id", "vat_id_validated"])

        with django_capture_on_commit_callbacks(execute=True):
            vies_service.clear_org_vat_fields(stripe_org)

        mock_delay.assert_called_once_with(str(stripe_org.id))

    @mock.patch("events.tasks.subscriptions.resync_org_subscription_fees.delay")
    def test_clearing_domestic_org_vat_fields_does_not_dispatch(
        self,
        mock_delay: mock.Mock,
        site_settings: SiteSettings,
        stripe_org: Organization,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Domestic AT org: grossed before AND after clearing (unknown-country fail-safe) — no resync."""
        stripe_org.vat_id = "ATU12345678"
        stripe_org.vat_id_validated = True
        stripe_org.save(update_fields=["vat_id", "vat_id_validated"])

        with django_capture_on_commit_callbacks(execute=True):
            vies_service.clear_org_vat_fields(stripe_org)

        mock_delay.assert_not_called()

    @mock.patch("events.tasks.subscriptions.resync_org_subscription_fees.delay")
    def test_billing_info_update_without_fee_effect_does_not_dispatch(
        self,
        mock_delay: mock.Mock,
        site_settings: SiteSettings,
        stripe_org: Organization,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        with django_capture_on_commit_callbacks(execute=True):
            vies_service.update_org_billing_info(stripe_org, {"billing_name": "New Name"})

        mock_delay.assert_not_called()


# ---- Celery task glue --------------------------------------------------------


class TestResyncTask:
    @mock.patch("events.service.subscription_stripe_service.stripe.Subscription.modify")
    def test_task_resolves_org_and_returns_counters(
        self,
        mock_modify: mock.Mock,
        site_settings: SiteSettings,
        stripe_org: Organization,
        online_plan: MembershipSubscriptionPlan,
    ) -> None:
        _make_sub(online_plan, "resync_task", stripe_subscription_id="sub_task")

        result = resync_org_subscription_fees(str(stripe_org.id))

        assert result == {"updated": 1, "skipped_schedule_managed": 0, "failed": 0}
        assert mock_modify.call_args.args[0] == "sub_task"
