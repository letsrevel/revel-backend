"""Eventbrite translator/client tests on recorded fixtures. No network."""

import json
import typing as t
from pathlib import Path

import httpx
import pytest
from django.test import RequestFactory

from integrations.enums import IntegrationErrorCode
from integrations.exceptions import ProviderError
from integrations.providers.base import (
    ListingProvider,
    ResolvedNotification,
    TokenSet,
    WebhookNotification,
)
from integrations.providers.eventbrite import client
from integrations.providers.eventbrite.provider import EventbriteProvider
from integrations.tests.recorder import Recorder

FIXTURES = Path(__file__).parent / "fixtures" / "eventbrite"


def _fixture(name: str) -> dict[str, t.Any]:
    return t.cast(dict[str, t.Any], json.loads((FIXTURES / f"{name}.json").read_text()))


def _provider(recorder: Recorder) -> EventbriteProvider:
    return EventbriteProvider(client_id="APPKEY", client_secret="SECRET", transport=recorder.transport())


def test_satisfies_protocol() -> None:
    assert isinstance(_provider(Recorder({})), ListingProvider)


def test_authorize_url_carries_state_and_redirect() -> None:
    url = _provider(Recorder({})).authorize_url("st4te", "https://api.example/cb")
    assert url.startswith("https://www.eventbrite.com/oauth/authorize?")
    assert "response_type=code" in url and "client_id=APPKEY" in url
    assert "state=st4te" in url and "redirect_uri=https%3A%2F%2Fapi.example%2Fcb" in url


def test_exchange_code_posts_form_and_returns_bare_token() -> None:
    rec = Recorder({("POST", "/oauth/token"): (200, {"access_token": "TOK", "token_type": "bearer"})})
    token = _provider(rec).exchange_code("c0de", "https://api.example/cb")
    assert token == TokenSet(access_token="TOK")
    req = rec.requests[0]
    assert req.headers["content-type"].startswith("application/x-www-form-urlencoded")
    body = req.content.decode()
    assert "grant_type=authorization_code" in body and "client_id=APPKEY" in body and "client_secret=SECRET" in body
    assert "code=c0de" in body and "redirect_uri=https%3A%2F%2Fapi.example%2Fcb" in body


def test_exchange_code_invalid_grant() -> None:
    rec = Recorder(
        {("POST", "/oauth/token"): (400, {"error": "invalid_grant", "error_description": "code is invalid or expired"})}
    )
    with pytest.raises(ProviderError) as exc:
        _provider(rec).exchange_code("bad", "https://api.example/cb")
    assert exc.value.code == IntegrationErrorCode.PROVIDER_REJECTED
    assert exc.value.provider_message == "code is invalid or expired"


def test_list_accounts_maps_organizations() -> None:
    rec = Recorder({("GET", "/v3/users/me/organizations/"): (200, _fixture("users_me_organizations"))})
    accounts = _provider(rec).list_accounts(TokenSet(access_token="TOK"))
    assert [a.remote_id for a in accounts] == ["3012894655993"]
    assert accounts[0].name
    assert rec.requests[0].headers["authorization"] == "Bearer TOK"


def test_401_maps_to_connection_revoked() -> None:
    rec = Recorder(
        {
            ("GET", "/v3/users/me/organizations/"): (
                401,
                {"error": "NOT_AUTHORIZED", "error_description": "The OAuth token you provided was invalid."},
            )
        }
    )
    with pytest.raises(ProviderError) as exc:
        _provider(rec).list_accounts(TokenSet(access_token="dead"))
    assert exc.value.code == IntegrationErrorCode.CONNECTION_REVOKED
    assert exc.value.retryable is False


def test_429_is_retryable_rate_limit() -> None:
    rec = Recorder(
        {("GET", "/v3/users/me/organizations/"): (429, {"error": "HIT_RATE_LIMIT", "error_description": "slow down"})}
    )
    with pytest.raises(ProviderError) as exc:
        _provider(rec).list_accounts(TokenSet(access_token="TOK"))
    assert exc.value.code == IntegrationErrorCode.PROVIDER_RATE_LIMITED
    assert exc.value.retryable is True


def test_404_maps_to_remote_event_missing() -> None:
    rec = Recorder({("GET", "/v3/events/1/"): (404, {"error": "NOT_FOUND", "error_description": "Event not found"})})
    with pytest.raises(ProviderError) as exc:
        _provider(rec).get_event(TokenSet(access_token="TOK"), "1")
    assert exc.value.code == IntegrationErrorCode.REMOTE_EVENT_MISSING


def test_non_json_error_body_maps_without_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="Bad Gateway")

    provider = EventbriteProvider(client_id="APPKEY", client_secret="SECRET", transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as exc:
        provider.list_accounts(TokenSet(access_token="TOK"))
    assert exc.value.code == IntegrationErrorCode.PROVIDER_REJECTED
    assert exc.value.provider_message is None
    assert exc.value.retryable is True


def test_non_json_success_body_never_escapes_as_value_error() -> None:
    """A 2xx with a non-JSON body must raise ``ProviderError``, not a bare ``ValueError``."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    provider = EventbriteProvider(client_id="APPKEY", client_secret="SECRET", transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as exc:
        provider.list_accounts(TokenSet(access_token="TOK"))
    assert exc.value.code == IntegrationErrorCode.PROVIDER_REJECTED


def test_success_body_missing_access_token_never_escapes_as_key_error() -> None:
    """A 2xx dict missing ``access_token`` must raise ``ProviderError``, not a bare ``KeyError``."""
    rec = Recorder({("POST", "/oauth/token"): (200, {})})
    with pytest.raises(ProviderError) as exc:
        _provider(rec).exchange_code("c0de", "https://api.example/cb")
    assert exc.value.code == IntegrationErrorCode.PROVIDER_REJECTED


def test_error_body_as_json_list_never_escapes_as_attribute_error() -> None:
    """An error body that is a JSON list must raise ``ProviderError``, not a bare ``AttributeError``."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=["unexpected"])

    provider = EventbriteProvider(client_id="APPKEY", client_secret="SECRET", transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as exc:
        provider.list_accounts(TokenSet(access_token="TOK"))
    assert exc.value.code == IntegrationErrorCode.PROVIDER_REJECTED
    assert exc.value.provider_message is None


def test_register_webhook_posts_actions_and_returns_id() -> None:
    rec = Recorder({("POST", "/v3/organizations/3012894655993/webhooks/"): (200, _fixture("webhook_create"))})
    wid = _provider(rec).register_webhook(
        TokenSet(access_token="TOK"), "3012894655993", "https://api.example/api/integrations/eventbrite/webhook/s3cret"
    )
    assert wid == "15933194"
    sent = json.loads(rec.requests[0].content)
    assert sent["endpoint_url"] == "https://api.example/api/integrations/eventbrite/webhook/s3cret"
    assert set(sent["actions"].split(",")) == {
        "order.placed",
        "order.refunded",
        "order.updated",
        "attendee.updated",
        "event.published",
        "event.unpublished",
    }


def test_unregister_webhook_deletes() -> None:
    rec = Recorder({("DELETE", "/v3/webhooks/15933194/"): (200, {"id": "15933194", "success": True})})
    _provider(rec).unregister_webhook(TokenSet(access_token="TOK"), "15933194")
    assert rec.requests[0].method == "DELETE"


def test_revoke_is_a_noop() -> None:
    rec = Recorder({})
    _provider(rec).revoke(TokenSet(access_token="TOK"))
    assert rec.requests == []


def test_parse_webhook_strips_host_and_reads_action_from_body() -> None:
    body = _fixture("webhook_delivery_event_published")
    request = RequestFactory().post(
        "/api/integrations/eventbrite/webhook/x",
        data=json.dumps(body),
        content_type="application/json",
        HTTP_X_EVENTBRITE_EVENT="event.published",
    )
    n = _provider(Recorder({})).parse_webhook(request)
    assert n.action == "event.published"
    assert n.resource_path == "/events/1999760883635/"
    body["config"].pop("endpoint_url")  # redacted (see the test below); everything else round-trips
    assert n.raw == body


def test_parse_webhook_redacts_the_endpoint_url() -> None:
    """``config.endpoint_url`` carries our webhook secret; the stored payload must not."""
    body = _fixture("webhook_delivery_event_published")
    assert "endpoint_url" in body["config"]  # the delivery really does echo it back
    request = RequestFactory().post("/x", data=json.dumps(body), content_type="application/json")
    n = _provider(Recorder({})).parse_webhook(request)
    assert "endpoint_url" not in n.raw["config"]
    assert "s3cret" not in json.dumps(n.raw)


def test_parse_webhook_rejects_foreign_host() -> None:
    body = {"api_url": "https://evil.example/v3/events/1/", "config": {"action": "order.placed"}}
    request = RequestFactory().post("/x", data=json.dumps(body), content_type="application/json")
    with pytest.raises(ProviderError) as exc:
        _provider(Recorder({})).parse_webhook(request)
    assert exc.value.code == IntegrationErrorCode.PROVIDER_REJECTED


def test_parse_webhook_rejects_non_default_port() -> None:
    body = {"api_url": "https://www.eventbriteapi.com:9999/v3/events/1/", "config": {"action": "order.placed"}}
    request = RequestFactory().post("/x", data=json.dumps(body), content_type="application/json")
    with pytest.raises(ProviderError) as exc:
        _provider(Recorder({})).parse_webhook(request)
    assert exc.value.code == IntegrationErrorCode.PROVIDER_REJECTED


def test_parse_webhook_rejects_malformed_port() -> None:
    body = {"api_url": "https://www.eventbriteapi.com:abc/v3/events/1/", "config": {"action": "order.placed"}}
    request = RequestFactory().post("/x", data=json.dumps(body), content_type="application/json")
    with pytest.raises(ProviderError) as exc:
        _provider(Recorder({})).parse_webhook(request)
    assert exc.value.code == IntegrationErrorCode.PROVIDER_REJECTED


def test_parse_webhook_rejects_malformed() -> None:
    request = RequestFactory().post("/x", data="not json", content_type="application/json")
    with pytest.raises(ProviderError):
        _provider(Recorder({})).parse_webhook(request)


def test_resolve_notification_ignores_a_vanished_order() -> None:
    """A 404 on the order fetch resolves to ``ignored``: there is no event to refresh, ever."""
    rec = Recorder({})  # every route 404s
    resolved = _provider(rec).resolve_notification(
        TokenSet(access_token="TOK"),
        WebhookNotification(action="order.placed", resource_path="/orders/123/", raw={}),
    )
    assert resolved == ResolvedNotification(remote_event_id=None, kind="ignored")
    assert [(r.method, r.url.path) for r in rec.requests] == [("GET", "/v3/orders/123/")]


def test_resolve_notification_reraises_other_order_failures() -> None:
    """Only ``remote_event_missing`` is swallowed; a 401 must still surface."""
    rec = Recorder({("GET", "/v3/orders/123/"): (401, {"error": "UNAUTHORIZED"})})
    with pytest.raises(ProviderError) as exc:
        _provider(rec).resolve_notification(
            TokenSet(access_token="TOK"),
            WebhookNotification(action="order.placed", resource_path="/orders/123/", raw={}),
        )
    assert exc.value.code == IntegrationErrorCode.CONNECTION_REVOKED


def test_budget_ttl_follows_the_bucket_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cached budget expires with the bucket it was read from, not an hour later."""
    calls: list[tuple[str, int, int]] = []

    def _set(key: str, value: int, timeout: int) -> None:
        calls.append((key, value, timeout))

    monkeypatch.setattr("integrations.providers.eventbrite.client.cache.set", _set)
    client._record_budget(
        httpx.Response(200, headers={"x-rate-limit": "key:APPKEY 1500/2000 reset=120s, token:TOK 10/2000"})
    )
    assert calls == [(client.BUDGET_CACHE_KEY, 500, 120)]


@pytest.mark.parametrize(
    ("header", "expected_ttl"),
    [
        ("key:APPKEY 1500/2000", client.BUDGET_CACHE_TTL),  # no reset reported
        ("key:APPKEY 1500/2000 reset=5s", client.BUDGET_CACHE_TTL_MIN),  # clamped up
        ("key:APPKEY 1500/2000 reset=99999s", client.BUDGET_CACHE_TTL),  # clamped down
    ],
)
def test_budget_ttl_is_clamped(header: str, expected_ttl: int, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        "integrations.providers.eventbrite.client.cache.set",
        lambda key, value, timeout: calls.append(timeout),
    )
    client._record_budget(httpx.Response(200, headers={"x-rate-limit": header}))
    assert calls == [expected_ttl]


def test_a_cache_outage_never_fails_a_successful_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Budget bookkeeping is advisory and runs after the response.

    Letting a Redis outage raise here would discard the body of a successful ``create_event`` —
    losing the new remote ID and duplicating the listing on the retry.
    """

    def _boom(key: str, value: int, timeout: int) -> None:
        raise ConnectionError("redis is down")

    monkeypatch.setattr("integrations.providers.eventbrite.client.cache.set", _boom)
    rec = Recorder({("POST", "/v3/organizations/acc-1/events/"): (200, {"id": "999", "url": "u", "status": "draft"})})

    body = client.EventbriteClient("TOK", transport=rec.transport()).request(
        "POST", "/organizations/acc-1/events/", json={}
    )

    assert body["id"] == "999"
