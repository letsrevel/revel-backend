"""Tests for the ``resync_subscription_fees`` management command."""

from decimal import Decimal
from io import StringIO
from unittest import mock

import pytest
import stripe
from django.core.management import CommandError, call_command

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def site_settings() -> SiteSettings:
    site = SiteSettings.get_solo()
    site.platform_vat_country = "AT"
    site.platform_vat_rate = Decimal("20.00")
    site.save()
    return site


@pytest.fixture
def stripe_org(organization: Organization) -> Organization:
    organization.stripe_account_id = "acct_test_org"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.platform_fee_percent = Decimal("1.50")
    organization.vat_country_code = "AT"
    organization.save()
    return organization


@pytest.fixture
def online_sub(stripe_org: Organization) -> MembershipSubscription:
    tier = MembershipTier.objects.get(organization=stripe_org, name="General membership")
    plan = MembershipSubscriptionPlan.objects.create(
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
    user = RevelUser.objects.create_user(username="cmd_sub", email="cmd_sub@example.com", password="pass")
    return MembershipSubscription.objects.create(
        user=user,
        plan=plan,
        organization=stripe_org,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        stripe_subscription_id="sub_cmd",
    )


def _run(*args: str) -> str:
    out = StringIO()
    call_command("resync_subscription_fees", *args, stdout=out)
    return out.getvalue()


@mock.patch("events.service.subscription_stripe_service.stripe.Subscription.modify")
def test_dry_run_reports_without_calling_stripe(
    mock_modify: mock.Mock,
    site_settings: SiteSettings,
    online_sub: MembershipSubscription,
) -> None:
    output = _run("--dry-run")

    mock_modify.assert_not_called()
    assert "would push 1.80% to 1 subscription(s)" in output
    assert "Done: 1 updated" in output


@mock.patch("events.service.subscription_stripe_service.stripe.Subscription.modify")
def test_live_run_pushes_percent(
    mock_modify: mock.Mock,
    site_settings: SiteSettings,
    online_sub: MembershipSubscription,
) -> None:
    output = _run("--sleep", "0")

    assert mock_modify.call_count == 1
    assert mock_modify.call_args.args[0] == "sub_cmd"
    assert mock_modify.call_args.kwargs["application_fee_percent"] == 1.80
    assert "Done: 1 updated, 0 schedule-managed skipped, 0 failed." in output


@mock.patch("events.service.subscription_stripe_service.stripe.Subscription.modify")
def test_null_subscription_id_rows_are_excluded(
    mock_modify: mock.Mock,
    site_settings: SiteSettings,
    online_sub: MembershipSubscription,
) -> None:
    """A mid-checkout row (NULL stripe_subscription_id) must never reach Stripe.

    ``exclude(stripe_subscription_id="")`` alone keeps NULL rows, which would
    blow up on ``Subscription.modify(None, ...)`` and abort the whole run.
    """
    user = RevelUser.objects.create_user(username="cmd_pending", email="cmd_pending@example.com", password="pass")
    MembershipSubscription.objects.create(
        user=user,
        plan=online_sub.plan,
        organization=online_sub.organization,
        status=MembershipSubscription.SubscriptionStatus.PENDING,
    )

    output = _run("--sleep", "0")

    assert mock_modify.call_count == 1
    assert mock_modify.call_args.args[0] == "sub_cmd"
    assert "Done: 1 updated, 0 schedule-managed skipped, 0 failed." in output


@mock.patch("events.service.subscription_stripe_service.stripe.Subscription.modify")
def test_failures_surface_as_command_error(
    mock_modify: mock.Mock,
    site_settings: SiteSettings,
    online_sub: MembershipSubscription,
) -> None:
    mock_modify.side_effect = stripe.error.APIError("boom")

    with pytest.raises(CommandError, match="1 subscription"):
        _run("--sleep", "0")


def test_unknown_org_slug_errors(site_settings: SiteSettings, online_sub: MembershipSubscription) -> None:
    with pytest.raises(CommandError, match="no-such-org"):
        _run("--org-slug", "no-such-org", "--dry-run")


@mock.patch("events.service.subscription_stripe_service.stripe.Subscription.modify")
def test_schedule_managed_rows_are_reported_skipped(
    mock_modify: mock.Mock,
    site_settings: SiteSettings,
    online_sub: MembershipSubscription,
) -> None:
    online_sub.stripe_schedule_id = "sub_sched_cmd"
    online_sub.save(update_fields=["stripe_schedule_id"])

    output = _run("--sleep", "0")

    mock_modify.assert_not_called()
    assert "1 schedule-managed skipped" in output
    assert "Re-run once pending downgrades release" in output
