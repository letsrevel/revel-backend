"""Untrusted pointers resolve into (remote_event_id, kind) using only the provider's own host."""

import json
import typing as t
from pathlib import Path

import httpx
import pytest
from django.core.cache import cache

from integrations.exceptions import ProviderError
from integrations.providers.base import TokenSet, WebhookNotification
from integrations.providers.eventbrite.client import BUDGET_CACHE_KEY
from integrations.tests.recorder import Recorder

FIXTURES = Path(__file__).parent / "fixtures" / "eventbrite"
TOKEN = TokenSet(access_token="TOK")


def _fixture(name: str) -> dict[str, t.Any]:
    return t.cast(dict[str, t.Any], json.loads((FIXTURES / f"{name}.json").read_text()))


def _n(action: str, path: str) -> WebhookNotification:
    return WebhookNotification(
        action=action,
        resource_path=path,
        raw={"api_url": f"https://www.eventbriteapi.com/v3{path}", "config": {"action": action}},
    )


@pytest.mark.parametrize(
    ("action", "kind"),
    [
        ("order.placed", "order_changed"),
        ("order.refunded", "order_changed"),
        ("order.updated", "order_changed"),
        ("attendee.updated", "order_changed"),
        ("event.published", "event_published"),
        ("event.unpublished", "event_unpublished"),
        ("venue.updated", "ignored"),
    ],
)
def test_kind_mapping(action: str, kind: str) -> None:
    rec = Recorder({})
    r = rec.provider().resolve_notification(TOKEN, _n(action, "/events/1999760883635/"))
    assert r.kind == kind
    assert r.remote_event_id == ("1999760883635" if kind != "ignored" else None)
    assert rec.requests == []  # event-path notifications never fetch


def test_attendee_path_carries_event_id_without_fetch() -> None:
    rec = Recorder({})
    r = rec.provider().resolve_notification(
        TOKEN, _n("attendee.updated", "/events/1999760883635/attendees/22824788917/")
    )
    assert (r.kind, r.remote_event_id) == ("order_changed", "1999760883635")
    assert rec.requests == []


def test_order_path_fetches_order_from_own_host() -> None:
    rec = Recorder({("GET", "/v3/orders/15614839853/"): (200, _fixture("order_resolved"))})
    r = rec.provider().resolve_notification(TOKEN, _n("order.placed", "/orders/15614839853/"))
    assert (r.kind, r.remote_event_id) == ("order_changed", "1999760883635")
    assert (
        rec.requests[0].url.host == "www.eventbriteapi.com" and rec.requests[0].headers["authorization"] == "Bearer TOK"
    )


def test_order_fetch_failure_propagates() -> None:
    rec = Recorder(
        {("GET", "/v3/orders/999999999/"): (401, {"error": "NOT_AUTHORIZED", "error_description": "bad token"})}
    )
    with pytest.raises(ProviderError):
        rec.provider().resolve_notification(TOKEN, _n("order.placed", "/orders/999999999/"))


def test_unknown_path_is_ignored() -> None:
    r = Recorder({}).provider().resolve_notification(TOKEN, _n("order.placed", "/venues/1/"))
    assert (r.kind, r.remote_event_id) == ("ignored", None)


def test_order_path_traversal_is_ignored_without_a_fetch() -> None:
    rec = Recorder({})
    r = rec.provider().resolve_notification(TOKEN, _n("order.placed", "/orders/..%2Fusers%2Fme/"))
    assert (r.kind, r.remote_event_id) == ("ignored", None)
    assert rec.requests == []


def test_client_records_key_budget_from_header() -> None:
    cache.delete(BUDGET_CACHE_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"organizations": []},
            headers={"x-rate-limit": "token:ABC 21/2000 reset=3575s, key:XYZ 1750/2000 reset=3575s"},
        )

    from integrations.providers.eventbrite.provider import EventbriteProvider

    p = EventbriteProvider(client_id="K", client_secret="S", transport=httpx.MockTransport(handler))
    p.list_accounts(TOKEN)
    assert cache.get(BUDGET_CACHE_KEY) == 250
    assert p.remaining_budget() == 250


def test_missing_or_malformed_header_leaves_budget_unknown() -> None:
    cache.delete(BUDGET_CACHE_KEY)
    rec = Recorder({("GET", "/v3/users/me/organizations/"): (200, {"organizations": []})})
    p = rec.provider()
    p.list_accounts(TOKEN)
    assert p.remaining_budget() is None
