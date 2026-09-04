"""Eventbrite translator/client tests on recorded fixtures. No network."""

import json
import typing as t
from pathlib import Path

import httpx
import pytest
from django.test import RequestFactory

from integrations.exceptions import ProviderError
from integrations.providers.base import ListingProvider, TokenSet
from integrations.providers.eventbrite.provider import EventbriteProvider
from integrations.schema import IntegrationErrorCode

FIXTURES = Path(__file__).parent / "fixtures" / "eventbrite"


def _fixture(name: str) -> dict[str, t.Any]:
    return t.cast(dict[str, t.Any], json.loads((FIXTURES / f"{name}.json").read_text()))


class Recorder:
    """httpx transport that answers from a route table and records every request."""

    def __init__(self, routes: dict[tuple[str, str], tuple[int, dict[str, t.Any]]]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        status, body = self.routes.get(key, (404, {"error": "NOT_FOUND", "error_description": "no route"}))
        return httpx.Response(status, json=body)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


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


def test_non_json_error_body_maps_without_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="Bad Gateway")

    provider = EventbriteProvider(client_id="APPKEY", client_secret="SECRET", transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as exc:
        provider.list_accounts(TokenSet(access_token="TOK"))
    assert exc.value.code == IntegrationErrorCode.PROVIDER_REJECTED
    assert exc.value.provider_message is None
    assert exc.value.retryable is True


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
    assert n.raw == body


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


def test_parse_webhook_rejects_malformed() -> None:
    request = RequestFactory().post("/x", data="not json", content_type="application/json")
    with pytest.raises(ProviderError):
        _provider(Recorder({})).parse_webhook(request)
