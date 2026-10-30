"""Tests for ``provision_stripe_webhooks --sync``.

Endpoints are built WITHOUT a ``connect`` attribute (the Stripe object has none) unless a test says otherwise.
"""

import json
import typing as t
from collections.abc import Iterator
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import CommandError, call_command

from events.management.commands.provision_stripe_webhooks import CONNECT_EVENTS, PLATFORM_EVENTS

URL = "https://api.example.com/api/stripe/webhook"
OLD_PLATFORM = tuple(e for e in PLATFORM_EVENTS if not e.startswith("charge.dispute."))
OLD_CONNECT = OLD_PLATFORM + ("account.updated",)
P_DESC = "Revel platform events"
C_DESC = "Revel Connect events"


def _ep(
    id_: str,
    events: t.Iterable[str],
    *,
    description: str | None = None,
    status: str = "enabled",
    url: str = URL,
    **extra: t.Any,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=id_, url=url, enabled_events=list(events), description=description, status=status, **extra
    )


@pytest.fixture
def stripe_mock() -> Iterator[MagicMock]:
    """Patch the command's ``stripe`` module and ``configure_stripe``."""
    with (
        patch("events.management.commands.provision_stripe_webhooks.stripe") as s,
        patch("events.management.commands.provision_stripe_webhooks.configure_stripe"),
    ):
        yield s


def _list(s: MagicMock, *eps: SimpleNamespace) -> None:
    s.WebhookEndpoint.list.return_value.auto_paging_iter.return_value = iter(eps)


def _canonical() -> tuple[SimpleNamespace, SimpleNamespace]:
    return (
        _ep("we_p", PLATFORM_EVENTS, description=P_DESC),
        _ep("we_c", CONNECT_EVENTS, description=C_DESC),
    )


def _old() -> tuple[SimpleNamespace, SimpleNamespace]:
    return (
        _ep("we_p", OLD_PLATFORM, description=P_DESC),
        _ep("we_c", OLD_CONNECT, description=C_DESC),
    )


def _run(**kwargs: t.Any) -> str:
    out = StringIO()
    call_command("provision_stripe_webhooks", url=URL, sync=True, stdout=out, **kwargs)
    return out.getvalue()


def test_sync_adds_missing_events_on_both(stripe_mock: MagicMock) -> None:
    """Missing dispute events are added on both endpoints with the full canonical list."""
    _list(stripe_mock, *_old())
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
    _list(stripe_mock, *_old())
    out = StringIO()
    call_command("provision_stripe_webhooks", url=URL, sync=True, dry_run=True, stdout=out)
    stripe_mock.WebhookEndpoint.list.assert_called_once()
    stripe_mock.WebhookEndpoint.modify.assert_not_called()
    assert "charge.dispute.created" in out.getvalue()


def test_sync_json_output(stripe_mock: MagicMock) -> None:
    """--format json prints only the per-endpoint diff."""
    _list(stripe_mock, _old()[0], _canonical()[1])
    out = StringIO()
    call_command("provision_stripe_webhooks", url=URL, sync=True, format="json", stdout=out)
    payload = json.loads(out.getvalue())
    assert payload["platform"]["id"] == "we_p"
    assert payload["platform"]["added"] == sorted(set(PLATFORM_EVENTS) - set(OLD_PLATFORM))
    assert payload["platform"]["removed"] == []
    assert payload["connect"]["added"] == payload["connect"]["removed"] == []
    assert payload["dry_run"] is False


def test_sync_ignores_other_urls(stripe_mock: MagicMock) -> None:
    """Endpoints at other URLs are neither counted nor modified."""
    other = _ep("we_other", ["*"], description=P_DESC, url="https://other.example.com/hook")
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
    with pytest.raises(CommandError, match="missing Connect") as exc:
        call_command("provision_stripe_webhooks", url=URL, sync=True)
    assert "without --sync" not in str(exc.value)


def test_sync_errors_on_duplicates(stripe_mock: MagicMock) -> None:
    """Two endpoints of the same kind at the URL are ambiguous."""
    platform, connect = _canonical()
    _list(stripe_mock, platform, connect, _ep("we_dup", PLATFORM_EVENTS, description=P_DESC))
    with pytest.raises(CommandError, match="duplicate platform") as exc:
        call_command("provision_stripe_webhooks", url=URL, sync=True)
    assert "we_dup" in str(exc.value)
    assert "--platform-id" in str(exc.value)
    stripe_mock.WebhookEndpoint.modify.assert_not_called()


def test_sync_with_force_is_rejected(stripe_mock: MagicMock) -> None:
    """--sync and --force are mutually exclusive."""
    with pytest.raises(CommandError, match="--force"):
        call_command("provision_stripe_webhooks", url=URL, sync=True, force=True)
    stripe_mock.WebhookEndpoint.list.assert_not_called()


def test_sync_refuses_wildcard(stripe_mock: MagicMock) -> None:
    """A wildcard subscription is never narrowed silently."""
    _list(stripe_mock, _ep("we_p", ["*"], description=P_DESC), _canonical()[1])
    with pytest.raises(CommandError, match="wildcard"):
        call_command("provision_stripe_webhooks", url=URL, sync=True)
    stripe_mock.WebhookEndpoint.modify.assert_not_called()


@pytest.mark.parametrize(
    ("p_kwargs", "c_kwargs", "basis"),
    [
        ({"connect": False}, {"connect": True}, "connect flag"),
        ({"description": P_DESC}, {"description": C_DESC}, "description"),
        ({}, {}, "account.updated"),
    ],
)
def test_sync_classification_bases(
    stripe_mock: MagicMock, p_kwargs: dict[str, t.Any], c_kwargs: dict[str, t.Any], basis: str
) -> None:
    """Platform vs Connect is derived from the flag, then the description, then the event list."""
    _list(stripe_mock, _ep("we_p", OLD_PLATFORM, **p_kwargs), _ep("we_c", OLD_CONNECT, **c_kwargs))
    output = _run()
    assert basis in output
    assert [c.args[0] for c in stripe_mock.WebhookEndpoint.modify.call_args_list] == ["we_p", "we_c"]


def test_sync_ambiguous_endpoints_suggest_id_flags(stripe_mock: MagicMock) -> None:
    """Two undistinguishable endpoints cannot be classified."""
    _list(stripe_mock, _ep("we_1", OLD_PLATFORM), _ep("we_2", OLD_PLATFORM), _ep("we_3", OLD_CONNECT))
    with pytest.raises(CommandError, match="--platform-id"):
        call_command("provision_stripe_webhooks", url=URL, sync=True)
    stripe_mock.WebhookEndpoint.modify.assert_not_called()


def test_sync_unclassifiable_wildcard_suggests_id_flags(stripe_mock: MagicMock) -> None:
    """A wildcard endpoint without a description cannot be classified."""
    _list(stripe_mock, _ep("we_w", ["*"]), _ep("we_c", OLD_CONNECT))
    with pytest.raises(CommandError, match="--platform-id"):
        call_command("provision_stripe_webhooks", url=URL, sync=True)


def test_sync_id_flags_pin_the_pair(stripe_mock: MagicMock) -> None:
    """--platform-id / --connect-id resolve otherwise ambiguous endpoints."""
    _list(stripe_mock, _ep("we_1", OLD_PLATFORM), _ep("we_2", OLD_PLATFORM), _ep("we_3", OLD_CONNECT))
    output = _run(platform_id="we_2", connect_id="we_3")
    assert [c.args[0] for c in stripe_mock.WebhookEndpoint.modify.call_args_list] == ["we_2", "we_3"]
    assert "--platform-id" in output


def test_sync_id_flag_must_be_at_url(stripe_mock: MagicMock) -> None:
    """A pinned id that targets another URL is rejected."""
    other = _ep("we_x", OLD_PLATFORM, url="https://other.example.com/hook")
    _list(stripe_mock, other, *_old())
    with pytest.raises(CommandError, match="we_x"):
        call_command("provision_stripe_webhooks", url=URL, sync=True, platform_id="we_x")
    stripe_mock.WebhookEndpoint.modify.assert_not_called()


def test_id_flags_require_sync() -> None:
    """The id flags only make sense with --sync."""
    with pytest.raises(CommandError, match="--sync"):
        call_command("provision_stripe_webhooks", url=URL, platform_id="we_p")


def test_sync_ignores_disabled_duplicates_and_lists_them(stripe_mock: MagicMock) -> None:
    """Disabled endpoints do not count towards the pair and are listed as ignored."""
    _list(stripe_mock, *_canonical(), _ep("we_off", PLATFORM_EVENTS, description=P_DESC, status="disabled"))
    output = _run()
    assert "ignored (disabled): we_off" in output
    assert "already in sync" in output
    stripe_mock.WebhookEndpoint.modify.assert_not_called()


def test_sync_only_disabled_candidate_is_an_error(stripe_mock: MagicMock) -> None:
    """A disabled-only slot is reported as such, never as 'already in sync'."""
    _list(stripe_mock, _ep("we_p", PLATFORM_EVENTS, description=P_DESC, status="disabled"), _canonical()[1])
    with pytest.raises(CommandError, match="disabled") as exc:
        call_command("provision_stripe_webhooks", url=URL, sync=True)
    assert "we_p" in str(exc.value)


def test_sync_partial_failure_reports_state(stripe_mock: MagicMock) -> None:
    """If the second modify fails, the error names what was applied and what failed."""
    _list(stripe_mock, *_old())
    stripe_mock.WebhookEndpoint.modify.side_effect = [None, RuntimeError("boom")]
    with pytest.raises(CommandError) as exc:
        call_command("provision_stripe_webhooks", url=URL, sync=True)
    msg = str(exc.value)
    assert "applied: we_p" in msg
    assert "failed: we_c" in msg
    assert "boom" in msg


def test_sync_first_failure_reports_nothing_applied(stripe_mock: MagicMock) -> None:
    """If the first modify fails nothing was applied."""
    _list(stripe_mock, *_old())
    stripe_mock.WebhookEndpoint.modify.side_effect = RuntimeError("boom")
    with pytest.raises(CommandError, match="applied: none"):
        call_command("provision_stripe_webhooks", url=URL, sync=True)


def test_sync_prints_removal_warning(stripe_mock: MagicMock) -> None:
    """Removals are called out prominently."""
    platform, connect = _canonical()
    platform.enabled_events = [*PLATFORM_EVENTS, "customer.created"]
    _list(stripe_mock, platform, connect)
    assert "WARNING: will remove" in _run(dry_run=True)


def test_sync_json_reports_dry_run_flag(stripe_mock: MagicMock) -> None:
    """The JSON object carries dry_run=true on a dry run."""
    _list(stripe_mock, *_old())
    payload = json.loads(_run(dry_run=True, format="json"))
    assert payload["dry_run"] is True
