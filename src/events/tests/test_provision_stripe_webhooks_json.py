import json
import typing as t
from io import StringIO
from unittest import mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError


def _fake_endpoint(eid: str, secret: str) -> t.Any:
    ep = mock.Mock()
    ep.id = eid
    ep.secret = secret
    return ep


def test_json_format_emits_parseable_secrets(settings: t.Any) -> None:
    settings.STRIPE_SECRET_KEY = "sk_test_x"
    out = StringIO()
    with mock.patch("events.management.commands.provision_stripe_webhooks.stripe") as s:
        s.WebhookEndpoint.list.return_value.auto_paging_iter.return_value = iter([])
        s.WebhookEndpoint.create.side_effect = [
            _fake_endpoint("we_plat", "whsec_plat"),
            _fake_endpoint("we_conn", "whsec_conn"),
        ]
        call_command(
            "provision_stripe_webhooks",
            "--url",
            "https://api.example.com/api/stripe/webhook",
            "--format",
            "json",
            stdout=out,
        )
    payload = json.loads(out.getvalue())
    assert payload["platform"]["secret"] == "whsec_plat"
    assert payload["connect"]["secret"] == "whsec_conn"


def test_dry_run_with_json_format_is_rejected() -> None:
    """--format json promises JSON-only output, so it must refuse --dry-run (which prints text)."""
    with pytest.raises(CommandError, match="--dry-run cannot be combined with --format json"):
        call_command(
            "provision_stripe_webhooks",
            "--url",
            "https://api.example.com/api/stripe/webhook",
            "--dry-run",
            "--format",
            "json",
        )
