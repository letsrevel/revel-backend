"""Tests for the provision_stripe_webhooks management command."""

from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.conf import settings
from django.core.management import CommandError, call_command

pytestmark = pytest.mark.django_db


def _endpoint(id_: str, url: str, secret: str = "whsec_x") -> MagicMock:
    ep = MagicMock()
    ep.id = id_
    ep.url = url
    ep.secret = secret
    return ep


@patch("events.management.commands.provision_stripe_webhooks.stripe")
def test_creates_both_endpoints(mock_stripe: MagicMock) -> None:
    """A clean account gets the platform + Connect pair, pinned to the settings version."""
    mock_stripe.WebhookEndpoint.list.return_value.auto_paging_iter.return_value = iter([])
    mock_stripe.WebhookEndpoint.create.side_effect = [
        _endpoint("we_platform", "https://api.example.com/api/stripe/webhook", "whsec_p"),
        _endpoint("we_connect", "https://api.example.com/api/stripe/webhook", "whsec_c"),
    ]
    out = StringIO()
    call_command("provision_stripe_webhooks", url="https://api.example.com/api/stripe/webhook", stdout=out)

    assert mock_stripe.WebhookEndpoint.create.call_count == 2
    connect_flags = [c.kwargs["connect"] for c in mock_stripe.WebhookEndpoint.create.call_args_list]
    assert connect_flags == [False, True]
    api_versions = {c.kwargs["api_version"] for c in mock_stripe.WebhookEndpoint.create.call_args_list}
    assert api_versions == {settings.STRIPE_API_VERSION}
    # The Connect endpoint additionally subscribes to account.updated.
    platform_events, connect_events = (
        c.kwargs["enabled_events"] for c in mock_stripe.WebhookEndpoint.create.call_args_list
    )
    assert "account.updated" not in platform_events
    assert "account.updated" in connect_events
    # Both secrets are printed for the operator (shown exactly once by Stripe).
    output = out.getvalue()
    assert "whsec_p" in output
    assert "whsec_c" in output


@patch("events.management.commands.provision_stripe_webhooks.stripe")
def test_refuses_when_url_already_provisioned(mock_stripe: MagicMock) -> None:
    """Without --force, an existing endpoint at the URL aborts the command."""
    existing = _endpoint("we_old", "https://api.example.com/api/stripe/webhook")
    mock_stripe.WebhookEndpoint.list.return_value.auto_paging_iter.return_value = iter([existing])
    with pytest.raises(CommandError):
        call_command("provision_stripe_webhooks", url="https://api.example.com/api/stripe/webhook")
    mock_stripe.WebhookEndpoint.create.assert_not_called()


@patch("events.management.commands.provision_stripe_webhooks.stripe")
def test_force_creates_alongside_existing(mock_stripe: MagicMock) -> None:
    """--force creates the new pair even when the old endpoint still exists (cutover overlap)."""
    existing = _endpoint("we_old", "https://api.example.com/api/stripe/webhook")
    mock_stripe.WebhookEndpoint.list.return_value.auto_paging_iter.return_value = iter([existing])
    mock_stripe.WebhookEndpoint.create.side_effect = [
        _endpoint("we_platform", "https://api.example.com/api/stripe/webhook", "whsec_p"),
        _endpoint("we_connect", "https://api.example.com/api/stripe/webhook", "whsec_c"),
    ]
    call_command("provision_stripe_webhooks", url="https://api.example.com/api/stripe/webhook", force=True)
    assert mock_stripe.WebhookEndpoint.create.call_count == 2


@patch("events.management.commands.provision_stripe_webhooks.stripe")
def test_dry_run_makes_no_api_calls(mock_stripe: MagicMock) -> None:
    """--dry-run previews both endpoints without touching the Stripe API."""
    out = StringIO()
    call_command(
        "provision_stripe_webhooks",
        url="https://api.example.com/api/stripe/webhook",
        dry_run=True,
        stdout=out,
    )
    mock_stripe.WebhookEndpoint.list.assert_not_called()
    mock_stripe.WebhookEndpoint.create.assert_not_called()
    output = out.getvalue()
    assert "DRY RUN" in output
    assert "account.updated" in output


def test_rejects_http_url() -> None:
    """Plain http URLs are refused outright."""
    with pytest.raises(CommandError):
        call_command("provision_stripe_webhooks", url="http://insecure.example.com/api/stripe/webhook")
