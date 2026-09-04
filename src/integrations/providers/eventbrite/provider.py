"""Eventbrite implementation of ``ListingProvider`` (connection + webhook half; sync arrives in phase 2)."""

import typing as t
from urllib.parse import urlencode, urlsplit

import httpx
import orjson
from django.http import HttpRequest

from integrations.exceptions import ProviderError
from integrations.providers.base import Capabilities, RemoteAccount, TokenSet, WebhookNotification
from integrations.providers.eventbrite.client import API_HOST, OAUTH_AUTHORIZE, EventbriteClient
from integrations.schema import IntegrationErrorCode

WEBHOOK_ACTIONS = (
    "order.placed",
    "order.refunded",
    "order.updated",
    "attendee.updated",
    "event.published",
    "event.unpublished",
)


class EventbriteProvider:
    key: t.ClassVar[str] = "eventbrite"
    display_name: t.ClassVar[str] = "Eventbrite"
    capabilities: t.ClassVar[Capabilities] = Capabilities(
        requires_end_time=True,
        requires_capacity=True,
        supports_structured_content=True,
        supports_unpublish_with_orders=False,
        single_currency_per_event=True,
    )

    def __init__(self, client_id: str, client_secret: str, *, transport: httpx.BaseTransport | None = None) -> None:
        """Initialize with Eventbrite OAuth credentials; ``transport`` swaps in a fake httpx transport for tests."""
        self.client_id = client_id
        self.client_secret = client_secret
        self._transport = transport

    def _client(self, token: TokenSet | None = None) -> EventbriteClient:
        return EventbriteClient(token.access_token if token else None, transport=self._transport)

    # -- connection -------------------------------------------------------------------
    def authorize_url(self, state: str, redirect_uri: str) -> str:
        """Consent URL. ``state`` round-trips verbatim even though Eventbrite's docs omit it (spec §14)."""
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "state": state,
        }
        return f"{OAUTH_AUTHORIZE}?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str) -> TokenSet:
        """Exchange an authorization code for a token set."""
        body = self._client().exchange_code(self.client_id, self.client_secret, code, redirect_uri)
        return TokenSet(access_token=str(body["access_token"]))

    def revoke(self, token: TokenSet) -> None:
        """Eventbrite has no revocation endpoint; the owner removes the app in their account settings."""

    def list_accounts(self, token: TokenSet) -> list[RemoteAccount]:
        """List remote accounts accessible with the given token."""
        body = self._client(token).request("GET", "/users/me/organizations/")
        return [
            RemoteAccount(remote_id=str(o["id"]), name=str(o.get("name") or o["id"]))
            for o in body.get("organizations", [])
        ]

    # -- webhooks ---------------------------------------------------------------------
    def register_webhook(self, token: TokenSet, account_id: str, url: str) -> str:
        """Register a webhook and return its remote ID."""
        body = self._client(token).request(
            "POST",
            f"/organizations/{account_id}/webhooks/",
            json={"endpoint_url": url, "actions": ",".join(WEBHOOK_ACTIONS)},
        )
        return str(body["id"])

    def unregister_webhook(self, token: TokenSet, remote_webhook_id: str) -> None:
        """Unregister a webhook by its remote ID."""
        self._client(token).request("DELETE", f"/webhooks/{remote_webhook_id}/")

    def parse_webhook(self, request: HttpRequest) -> WebhookNotification:
        """Unsigned pointer body: keep the action and the *path* of ``api_url``.

        Refuses foreign hosts (SSRF guard, spec §8).
        """
        try:
            raw = t.cast(dict[str, t.Any], orjson.loads(request.body or b""))
            api_url = str(raw["api_url"])
            action = str(raw["config"]["action"])
            parts = urlsplit(api_url)
            port_ok = parts.port in (None, 443)
        except (orjson.JSONDecodeError, KeyError, TypeError, AttributeError, ValueError) as e:
            raise ProviderError(IntegrationErrorCode.PROVIDER_REJECTED, "malformed webhook body") from e
        if parts.scheme != "https" or parts.hostname != API_HOST or not port_ok or not parts.path.startswith("/v3/"):
            raise ProviderError(IntegrationErrorCode.PROVIDER_REJECTED, "unexpected resource host")
        return WebhookNotification(action=action, resource_path=parts.path.removeprefix("/v3"), raw=raw)
