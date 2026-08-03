"""Tests for the ``scrub_stripe_products`` management command."""

import typing as t
from decimal import Decimal
from io import StringIO
from unittest import mock

import pytest
import stripe
from django.core.management import call_command

from accounts.models import RevelUser
from events.models import MembershipSubscriptionPlan, MembershipTier, Organization

pytestmark = pytest.mark.django_db

_CMD = "events.management.commands.scrub_stripe_products"


@pytest.fixture
def stripe_org(organization: Organization) -> Organization:
    organization.stripe_account_id = "acct_scrub_org"
    organization.save(update_fields=["stripe_account_id"])
    return organization


@pytest.fixture
def online_plan(stripe_org: Organization) -> MembershipSubscriptionPlan:
    tier = MembershipTier.objects.create(organization=stripe_org, name="Gold")
    return MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit="month",
        period_count=1,
        payment_method=MembershipSubscriptionPlan.PaymentMethod.ONLINE,
        stripe_product_id="prod_plan",
        stripe_price_id="price_plan",
    )


def _run(*args: str) -> str:
    out = StringIO()
    call_command("scrub_stripe_products", *args, stdout=out)
    return out.getvalue()


def _list_result(products: list[dict[str, str]]) -> mock.Mock:
    """Build a ``stripe.Product.list`` return value whose pages yield ``products``."""
    result = mock.Mock()
    result.auto_paging_iter.return_value = iter(products)
    return result


def _catalog(per_account: dict[str | None, list[dict[str, str]]]) -> t.Callable[..., mock.Mock]:
    """Side effect for ``stripe.Product.list``: per-account product pages.

    Keys are ``stripe_account`` kwarg values; ``None`` is the platform account
    (no Connect header). Accounts not listed yield an empty catalog. A fresh
    iterator is built per call — the sweep visits several accounts.
    """

    def _side_effect(**kwargs: t.Any) -> mock.Mock:
        return _list_result(per_account.get(kwargs.get("stripe_account"), []))

    return _side_effect


@mock.patch(f"{_CMD}.stripe.Product.list")
@mock.patch(f"{_CMD}.stripe.Product.modify")
def test_scrubs_plan_products(
    mock_modify: mock.Mock,
    mock_list: mock.Mock,
    online_plan: MembershipSubscriptionPlan,
) -> None:
    """A plan with a Stripe product gets a generic name and an emptied description."""
    mock_list.side_effect = _catalog({})

    output = _run()

    assert mock_modify.call_count == 1
    assert mock_modify.call_args.args == ("prod_plan",)
    assert mock_modify.call_args.kwargs == {
        "name": "Membership",
        "description": "",
        "stripe_account": "acct_scrub_org",
    }
    assert "1 plan product(s) scrubbed" in output


@mock.patch(f"{_CMD}.stripe.Product.list")
@mock.patch(f"{_CMD}.stripe.Product.modify")
def test_sweep_renames_prefixed_products(
    mock_modify: mock.Mock,
    mock_list: mock.Mock,
    stripe_org: Organization,
) -> None:
    """Ad-hoc catalog products with organizer-authored prefixes are renamed."""
    mock_list.side_effect = _catalog(
        {
            "acct_scrub_org": [
                {"id": "prod_1", "name": "Ticket: Kinky Party (VIP)"},
                {"id": "prod_2", "name": "Season pass: Autumn 2026"},
            ]
        }
    )

    output = _run()

    assert mock.call(limit=100, stripe_account="acct_scrub_org") in mock_list.call_args_list
    assert mock_modify.call_args_list == [
        mock.call("prod_1", name="Ticket", stripe_account="acct_scrub_org"),
        mock.call("prod_2", name="Season pass", stripe_account="acct_scrub_org"),
    ]
    assert "2 product(s) renamed" in output
    assert "2 account(s) swept" in output  # the org account + the platform account


@mock.patch(f"{_CMD}.stripe.Product.list")
@mock.patch(f"{_CMD}.stripe.Product.modify")
def test_platform_catalog_always_swept(
    mock_modify: mock.Mock,
    mock_list: mock.Mock,
    stripe_org: Organization,
) -> None:
    """The platform's own catalog is swept even when no org row references it.

    Orgs that once used the platform account (bootstrap data, later migrated or
    deleted) left ad-hoc Products there; the sweep must not depend on an org row
    still carrying ``settings.STRIPE_ACCOUNT``.
    """
    mock_list.side_effect = _catalog({None: [{"id": "prod_platform", "name": "Ticket: Orphaned Event"}]})

    output = _run()

    assert mock.call(limit=100) in mock_list.call_args_list  # no stripe_account header
    assert mock.call("prod_platform", name="Ticket") in mock_modify.call_args_list
    assert "1 product(s) renamed" in output
    assert "2 account(s) swept" in output


@mock.patch(f"{_CMD}.stripe.Product.list")
@mock.patch(f"{_CMD}.stripe.Product.modify")
def test_sweep_skips_generic_names(
    mock_modify: mock.Mock,
    mock_list: mock.Mock,
    stripe_org: Organization,
) -> None:
    """Already-generic or unrelated product names are left untouched."""
    mock_list.side_effect = _catalog(
        {
            "acct_scrub_org": [
                {"id": "prod_1", "name": "Ticket"},
                {"id": "prod_2", "name": "Season pass"},
                {"id": "prod_3", "name": "Merch bundle"},
            ]
        }
    )

    output = _run()

    mock_modify.assert_not_called()
    assert "0 product(s) renamed" in output


@mock.patch(f"{_CMD}.stripe.Product.list")
@mock.patch(f"{_CMD}.stripe.Product.modify")
def test_dry_run_makes_no_calls(
    mock_modify: mock.Mock,
    mock_list: mock.Mock,
    online_plan: MembershipSubscriptionPlan,
) -> None:
    """``--dry-run`` lists every intended modification without calling ``modify``."""
    mock_list.side_effect = _catalog({"acct_scrub_org": [{"id": "prod_1", "name": "Ticket: Kinky Party (VIP)"}]})

    output = _run("--dry-run")

    mock_modify.assert_not_called()
    assert "prod_plan" in output
    assert "prod_1" in output
    assert "[dry-run]" in output


@mock.patch(f"{_CMD}.stripe.Product.list")
@mock.patch(f"{_CMD}.stripe.Product.modify")
def test_stripe_error_continues(
    mock_modify: mock.Mock,
    mock_list: mock.Mock,
    stripe_org: Organization,
) -> None:
    """A dead account is logged and skipped; the sweep still reaches the others."""
    owner = RevelUser.objects.create_user(username="scrub_owner2", email="scrub2@example.com", password="pass")
    Organization.objects.create(name="Org Two", slug="org-two", owner=owner, stripe_account_id="acct_scrub_dead")

    def _list_side_effect(**kwargs: t.Any) -> mock.Mock:
        if kwargs.get("stripe_account") == "acct_scrub_dead":
            raise stripe.error.APIError("boom")
        return _catalog({"acct_scrub_org": [{"id": "prod_1", "name": "Ticket: Kinky Party (VIP)"}]})(**kwargs)

    mock_list.side_effect = _list_side_effect
    err = StringIO()
    out = StringIO()

    call_command("scrub_stripe_products", stdout=out, stderr=err)
    output = out.getvalue()

    mock_modify.assert_called_once_with("prod_1", name="Ticket", stripe_account="acct_scrub_org")
    assert "acct_scrub_dead" in err.getvalue()
    assert "2 account(s) swept" in output  # the live org account + the platform account
    assert "1 error(s)" in output
