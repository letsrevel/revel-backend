"""The backfill command pre-fills structured org billing addresses from Stripe."""

from io import StringIO
from unittest.mock import Mock, patch

import pytest
import stripe
from django.core.management import call_command

from events.models import Organization

pytestmark = pytest.mark.django_db

_ACCOUNT = {
    "company": {
        "address": {
            "line1": "Secretstrasse 9",
            "postal_code": "1010",
            "city": "Wien",
            "country": "AT",
        }
    }
}


@pytest.fixture
def stripe_org(organization: Organization) -> Organization:
    organization.stripe_account_id = "acct_backfill"
    organization.save(update_fields=["stripe_account_id", "updated_at"])
    return organization


def _run(*args: str) -> str:
    out = StringIO()
    call_command("backfill_org_billing_addresses", *args, stdout=out)
    return out.getvalue()


@patch("stripe.Account.retrieve")
def test_backfill_dry_run_writes_nothing(mock_retrieve: Mock, stripe_org: Organization) -> None:
    mock_retrieve.return_value = _ACCOUNT

    output = _run("--dry-run")

    stripe_org.refresh_from_db()
    assert stripe_org.billing_address_line1 == ""
    assert f"{stripe_org.id} filled" in output


@patch("stripe.Account.retrieve")
def test_backfill_fills_from_stripe(mock_retrieve: Mock, stripe_org: Organization) -> None:
    mock_retrieve.return_value = _ACCOUNT

    output = _run()

    stripe_org.refresh_from_db()
    assert stripe_org.billing_address_line1 == "Secretstrasse 9"
    assert stripe_org.billing_city == "Wien"
    assert stripe_org.billing_country_code == "AT"
    assert f"{stripe_org.id} filled" in output


@patch("stripe.Account.retrieve")
def test_backfill_falls_back_to_vat_country(mock_retrieve: Mock, stripe_org: Organization) -> None:
    mock_retrieve.return_value = {}
    stripe_org.vat_country_code = "DE"
    stripe_org.save(update_fields=["vat_country_code", "updated_at"])

    output = _run()

    stripe_org.refresh_from_db()
    assert stripe_org.billing_country_code == "DE"
    assert stripe_org.billing_address_line1 == ""
    assert f"{stripe_org.id} country_only" in output


@patch("stripe.Account.retrieve")
def test_backfill_nothing_to_fill_is_skipped(mock_retrieve: Mock, stripe_org: Organization) -> None:
    mock_retrieve.return_value = {}

    assert f"{stripe_org.id} skipped" in _run()


@patch("stripe.Account.retrieve")
def test_backfill_stripe_error_is_reported_and_continues(mock_retrieve: Mock, stripe_org: Organization) -> None:
    mock_retrieve.side_effect = stripe.error.APIError("boom")

    assert f"{stripe_org.id} error" in _run()


@patch("stripe.Account.retrieve")
def test_backfill_ignores_orgs_without_stripe_or_with_address(
    mock_retrieve: Mock, organization: Organization, stripe_org: Organization
) -> None:
    stripe_org.billing_address_line1 = "Already 1"
    stripe_org.save(update_fields=["billing_address_line1", "updated_at"])
    organization.stripe_account_id = ""
    organization.save(update_fields=["stripe_account_id", "updated_at"])

    _run()

    mock_retrieve.assert_not_called()


@patch("stripe.Account.retrieve")
def test_backfill_output_has_no_pii(mock_retrieve: Mock, stripe_org: Organization) -> None:
    mock_retrieve.return_value = _ACCOUNT

    output = _run()

    assert str(stripe_org.id) in output
    assert "filled" in output
    for secret in ("Secretstrasse", "Wien", stripe_org.name, "acct_backfill"):
        assert secret not in output
