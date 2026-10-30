"""Tests for ``provision_stripe_webhooks --sync``."""

import json
import typing as t
from collections.abc import Iterator
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import CommandError, call_command

from events.management.commands.provision_stripe_webhooks import CONNECT_EVENTS, PLATFORM_EVENTS

URL = "https://api.example.com/api/stripe/webhook"
OLD_PLATFORM = tuple(e for e in PLATFORM_EVENTS if not e.startswith("charge.dispute."))
OLD_CONNECT = OLD_PLATFORM + ("account.updated",)


def _ep(id_: str, events: t.Iterable[str], *, connect: bool, url: str = URL) -> MagicMock:
    ep = MagicMock()
    ep.id = id_
    ep.url = url
    ep.connect = connect
    ep.enabled_events = list(events)
    return ep


@pytest.fixture
def stripe_mock() -> Iterator[MagicMock]:
    """Patch the command's ``stripe`` module and ``configure_stripe``."""
    with (
        patch("events.management.commands.provision_stripe_webhooks.stripe") as s,
        patch("events.management.commands.provision_stripe_webhooks.configure_stripe"),
    ):
        yield s


def _list(s: MagicMock, *eps: MagicMock) -> None:
    s.WebhookEndpoint.list.return_value.auto_paging_iter.return_value = iter(eps)


def _canonical() -> tuple[MagicMock, MagicMock]:
    return (
        _ep("we_p", PLATFORM_EVENTS, connect=False),
        _ep("we_c", CONNECT_EVENTS, connect=True),
    )


def test_sync_adds_missing_events_on_both(stripe_mock: MagicMock) -> None:
    """Missing dispute events are added on both endpoints with the full canonical list."""
    _list(stripe_mock, _ep("we_p", OLD_PLATFORM, connect=False), _ep("we_c", OLD_CONNECT, connect=True))
    out = StringIO()
    call_command("provision_stripe_webhooks", url=URL, sync=True, stdout=out)

    calls = stripe_mock.WebhookEndpoint.modify.call_args_list
    assert [(c.args[0], c.kwargs["enabled_events"]) for c in calls] == [
        ("we_p", list(PLATFORM_EVENTS)),
        ("we_c", list(CONNECT_EVENTS)),
    ]
    output = out.getvalue()
    assert "charge.dispute.created" in output
    assert "we_p" in output
    assert "whsec" not in output


def test_sync_already_in_sync_makes_no_modify(stripe_mock: MagicMock) -> None:
    """Matching endpoints produce no modify call."""
    _list(stripe_mock, *_canonical())
    out = StringIO()
    call_command("provision_stripe_webhooks", url=URL, sync=True, stdout=out)
    stripe_mock.WebhookEndpoint.modify.assert_not_called()
    assert "already in sync" in out.getvalue()


def test_sync_removes_stale_event(stripe_mock: MagicMock) -> None:
    """An event type outside the canonical list is removed."""
    platform, connect = _canonical()
    platform.enabled_events = [*PLATFORM_EVENTS, "customer.created"]
    _list(stripe_mock, platform, connect)
    out = StringIO()
    call_command("provision_stripe_webhooks", url=URL, sync=True, stdout=out)

    stripe_mock.WebhookEndpoint.modify.assert_called_once_with("we_p", enabled_events=list(PLATFORM_EVENTS))
    assert "customer.created" in out.getvalue()


def test_sync_dry_run_does_not_modify(stripe_mock: MagicMock) -> None:
    """--sync --dry-run lists and prints the diff but never modifies."""
    _list(stripe_mock, _ep("we_p", OLD_PLATFORM, connect=False), _ep("we_c", OLD_CONNECT, connect=True))
    out = StringIO()
    call_command("provision_stripe_webhooks", url=URL, sync=True, dry_run=True, stdout=out)
    stripe_mock.WebhookEndpoint.list.assert_called_once()
    stripe_mock.WebhookEndpoint.modify.assert_not_called()
    assert "charge.dispute.created" in out.getvalue()


def test_sync_json_output(stripe_mock: MagicMock) -> None:
    """--format json prints only the per-endpoint diff."""
    _list(stripe_mock, _ep("we_p", OLD_PLATFORM, connect=False), _ep("we_c", CONNECT_EVENTS, connect=True))
    out = StringIO()
    call_command("provision_stripe_webhooks", url=URL, sync=True, format="json", stdout=out)
    payload = json.loads(out.getvalue())
    assert payload["platform"]["id"] == "we_p"
    assert payload["platform"]["added"] == sorted(set(PLATFORM_EVENTS) - set(OLD_PLATFORM))
    assert payload["platform"]["removed"] == []
    assert payload["connect"] == {"id": "we_c", "added": [], "removed": []}


def test_sync_ignores_other_urls(stripe_mock: MagicMock) -> None:
    """Endpoints at other URLs are neither counted nor modified."""
    other = _ep("we_other", ["*"], connect=False, url="https://other.example.com/hook")
    _list(stripe_mock, other, *_canonical())
    call_command("provision_stripe_webhooks", url=URL, sync=True, stdout=StringIO())
    stripe_mock.WebhookEndpoint.modify.assert_not_called()


def test_sync_errors_when_no_endpoints(stripe_mock: MagicMock) -> None:
    """No endpoints at the URL points the operator at plain provisioning."""
    _list(stripe_mock)
    with pytest.raises(CommandError, match="without --sync"):
        call_command("provision_stripe_webhooks", url=URL, sync=True)


def test_sync_errors_when_one_kind_missing(stripe_mock: MagicMock) -> None:
    """A missing Connect endpoint is an error."""
    _list(stripe_mock, _canonical()[0])
    with pytest.raises(CommandError, match="without --sync"):
        call_command("provision_stripe_webhooks", url=URL, sync=True)


def test_sync_errors_on_duplicates(stripe_mock: MagicMock) -> None:
    """Two endpoints of the same kind at the URL are ambiguous."""
    platform, connect = _canonical()
    _list(stripe_mock, platform, connect, _ep("we_dup", PLATFORM_EVENTS, connect=False))
    with pytest.raises(CommandError, match="we_dup") as exc:
        call_command("provision_stripe_webhooks", url=URL, sync=True)
    assert "disable the extra" in str(exc.value)
    stripe_mock.WebhookEndpoint.modify.assert_not_called()


def test_sync_with_force_is_rejected(stripe_mock: MagicMock) -> None:
    """--sync and --force are mutually exclusive."""
    with pytest.raises(CommandError, match="--force"):
        call_command("provision_stripe_webhooks", url=URL, sync=True, force=True)
    stripe_mock.WebhookEndpoint.list.assert_not_called()


def test_sync_refuses_wildcard(stripe_mock: MagicMock) -> None:
    """A wildcard subscription is never narrowed silently."""
    _list(stripe_mock, _ep("we_p", ["*"], connect=False), _canonical()[1])
    with pytest.raises(CommandError, match="wildcard"):
        call_command("provision_stripe_webhooks", url=URL, sync=True)
    stripe_mock.WebhookEndpoint.modify.assert_not_called()
