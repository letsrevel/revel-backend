"""Eventbrite implementation of ``ListingProvider`` (connection + webhook half; sync arrives in phase 2)."""

import typing as t
from urllib.parse import urlencode, urlsplit

import httpx
import orjson
from django.http import HttpRequest

from integrations.exceptions import ProviderError
from integrations.providers.base import (
    Capabilities,
    RemoteAccount,
    RemoteEvent,
    RemoteEventRef,
    RemoteEventSummary,
    RemoteTicketClass,
    TokenSet,
    WebhookNotification,
)
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
        try:
            return TokenSet(access_token=str(body["access_token"]))
        except (KeyError, TypeError) as e:
            raise ProviderError(IntegrationErrorCode.PROVIDER_REJECTED, "unexpected response shape") from e

    def revoke(self, token: TokenSet) -> None:
        """Eventbrite has no revocation endpoint; the owner removes the app in their account settings."""

    def list_accounts(self, token: TokenSet) -> list[RemoteAccount]:
        """List remote accounts accessible with the given token."""
        body = self._client(token).request("GET", "/users/me/organizations/")
        try:
            return [
                RemoteAccount(remote_id=str(o["id"]), name=str(o.get("name") or o["id"]))
                for o in body.get("organizations", [])
            ]
        except (KeyError, TypeError) as e:
            raise ProviderError(IntegrationErrorCode.PROVIDER_REJECTED, "unexpected response shape") from e

    # -- webhooks ---------------------------------------------------------------------
    def register_webhook(self, token: TokenSet, account_id: str, url: str) -> str:
        """Register a webhook and return its remote ID."""
        body = self._client(token).request(
            "POST",
            f"/organizations/{account_id}/webhooks/",
            json={"endpoint_url": url, "actions": ",".join(WEBHOOK_ACTIONS)},
        )
        try:
            return str(body["id"])
        except (KeyError, TypeError) as e:
            raise ProviderError(IntegrationErrorCode.PROVIDER_REJECTED, "unexpected response shape") from e

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

    # -- read -----------------------------------------------------------------
    def list_events(self, token: TokenSet, account_id: str) -> list[RemoteEventSummary]:
        """List remote events in an account."""
        raise NotImplementedError("phase 2 task 5/6")

    def get_event(self, token: TokenSet, remote_id: str) -> RemoteEvent:
        """Fetch a remote event including ticket classes and venue."""
        raise NotImplementedError("phase 2 task 5/6")

    # -- write ----------------------------------------------------------------
    def create_event(self, token: TokenSet, account_id: str, event: RemoteEvent) -> RemoteEventRef:
        """Create a remote event."""
        raise NotImplementedError("phase 2 task 5/6")

    def update_event(self, token: TokenSet, remote_id: str, event: RemoteEvent) -> RemoteEventRef:
        """Update a remote event."""
        raise NotImplementedError("phase 2 task 5/6")

    def set_description(self, token: TokenSet, remote_id: str, html: str) -> None:
        """Set long-form description (structured content on some platforms)."""
        raise NotImplementedError("phase 2 task 5/6")

    def publish_event(self, token: TokenSet, remote_id: str) -> None:
        """Publish a remote event."""
        raise NotImplementedError("phase 2 task 5/6")

    def cancel_event(self, token: TokenSet, remote_id: str) -> None:
        """Cancel a remote event."""
        raise NotImplementedError("phase 2 task 5/6")

    def upsert_ticket_class(self, token: TokenSet, remote_event_id: str, tc: RemoteTicketClass) -> str:
        """Create or update a ticket class, returning its remote ID."""
        raise NotImplementedError("phase 2 task 5/6")

    def delete_ticket_class(self, token: TokenSet, remote_event_id: str, remote_id: str) -> None:
        """Delete a ticket class."""
        raise NotImplementedError("phase 2 task 5/6")

    def set_ticket_class_paused(self, token: TokenSet, remote_event_id: str, remote_id: str, paused: bool) -> None:
        """Pause (hide) or unpause a ticket class."""
        raise NotImplementedError("phase 2 task 5/6")
