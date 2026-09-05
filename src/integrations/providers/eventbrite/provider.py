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
    RemoteVenue,
    TokenSet,
    WebhookNotification,
)
from integrations.providers.eventbrite import translate as tr
from integrations.providers.eventbrite.client import API_HOST, OAUTH_AUTHORIZE, EventbriteClient
from integrations.schema import IntegrationErrorCode

# ponytail: 20-page cap on list_events pagination — an org with >1000 draft/live/started events
# (at page_size 50) would need a real "sync in batches" design; not worth building speculatively.
MAX_LIST_PAGES = 20

T = t.TypeVar("T")

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
        """List remote events in an account, following ``pagination.continuation``."""
        client = self._client(token)
        params: dict[str, t.Any] = {"status": "draft,live,started", "order_by": "start_asc"}
        summaries: list[RemoteEventSummary] = []
        for _ in range(MAX_LIST_PAGES):
            body = client.request("GET", f"/organizations/{account_id}/events/", params=params)
            try:
                summaries.extend(tr.from_eventbrite_summary(e) for e in body.get("events", []))
                pagination = body.get("pagination") or {}
                if not pagination.get("has_more_items"):
                    break
                params["continuation"] = pagination["continuation"]
            except (KeyError, TypeError, ValueError) as e:
                raise ProviderError(IntegrationErrorCode.PROVIDER_REJECTED, "unexpected response shape") from e
        return summaries

    def get_event(self, token: TokenSet, remote_id: str) -> RemoteEvent:
        """Fetch a remote event including ticket classes and venue."""
        body = self._client(token).request("GET", f"/events/{remote_id}/", params={"expand": "venue,ticket_classes"})
        try:
            return tr.from_eventbrite_event(body)
        except (KeyError, TypeError, ValueError) as e:
            raise ProviderError(IntegrationErrorCode.PROVIDER_REJECTED, "unexpected response shape") from e

    # -- write ----------------------------------------------------------------
    def _shape(self, fn: t.Callable[[], T]) -> T:
        try:
            return fn()
        except (KeyError, TypeError, ValueError) as e:
            raise ProviderError(IntegrationErrorCode.PROVIDER_REJECTED, "unexpected response shape") from e

    def _create_venue(self, token: TokenSet, account_id: str, venue: RemoteVenue) -> str:
        body = self._client(token).request(
            "POST", f"/organizations/{account_id}/venues/", json=tr.to_eventbrite_venue(venue)
        )
        return self._shape(lambda: str(body["id"]))

    def _ref(self, body: dict[str, t.Any]) -> RemoteEventRef:
        return self._shape(
            lambda: RemoteEventRef(
                remote_id=str(body["id"]),
                url=str(body.get("url") or ""),
                status=tr.status_from_eventbrite(str(body.get("status") or "draft")),
            )
        )

    def create_event(self, token: TokenSet, account_id: str, event: RemoteEvent) -> RemoteEventRef:
        """Create a remote event, creating its venue first when one is set."""
        venue_id = self._create_venue(token, account_id, event.venue) if event.venue else None
        body = self._client(token).request(
            "POST", f"/organizations/{account_id}/events/", json=tr.to_eventbrite_event(event, venue_id=venue_id)
        )
        return self._ref(body)

    def update_event(self, token: TokenSet, remote_id: str, event: RemoteEvent) -> RemoteEventRef:
        """Update a remote event, updating the venue it already has in place instead of adding one."""
        venue_id = None
        if event.venue:
            current = self._client(token).request("GET", f"/events/{remote_id}/")
            venue_id = str(current.get("venue_id") or "") or None
            if venue_id:
                self._client(token).request("POST", f"/venues/{venue_id}/", json=tr.to_eventbrite_venue(event.venue))
            else:
                org = self._shape(lambda: str(current["organization_id"]))
                venue_id = self._create_venue(token, org, event.venue)
        body = self._client(token).request(
            "POST", f"/events/{remote_id}/", json=tr.to_eventbrite_event(event, venue_id=venue_id)
        )
        return self._ref(body)

    def set_description(self, token: TokenSet, remote_id: str, html: str) -> None:
        """Set long-form description via the structured-content endpoint."""
        version = "1"
        try:
            current = self._client(token).request("GET", f"/events/{remote_id}/structured_content/")
            version = str(current.get("page_version_number") or "1")
        except ProviderError as e:
            if e.code != IntegrationErrorCode.REMOTE_EVENT_MISSING:  # 404 = no content yet; anything else is real
                raise
        self._client(token).request(
            "POST", f"/events/{remote_id}/structured_content/{version}/", json=tr.to_eventbrite_structured_content(html)
        )

    def publish_event(self, token: TokenSet, remote_id: str) -> None:
        """Publish a remote event."""
        self._client(token).request("POST", f"/events/{remote_id}/publish/")

    def cancel_event(self, token: TokenSet, remote_id: str) -> None:
        """Cancel a remote event."""
        self._client(token).request("POST", f"/events/{remote_id}/cancel/")

    def upsert_ticket_class(self, token: TokenSet, remote_event_id: str, tc: RemoteTicketClass) -> str:
        """Create or update a ticket class, returning its remote ID."""
        path = f"/events/{remote_event_id}/ticket_classes/" + (f"{tc.remote_id}/" if tc.remote_id else "")
        body = self._client(token).request("POST", path, json=tr.to_eventbrite_ticket_class(tc))
        return self._shape(lambda: str(body["id"]))

    def delete_ticket_class(self, token: TokenSet, remote_event_id: str, remote_id: str) -> None:
        """Delete a ticket class. A 404 (already gone) counts as success."""
        try:
            self._client(token).request("DELETE", f"/events/{remote_event_id}/ticket_classes/{remote_id}/")
        except ProviderError as e:
            if e.code != IntegrationErrorCode.REMOTE_EVENT_MISSING:
                raise

    def set_ticket_class_paused(self, token: TokenSet, remote_event_id: str, remote_id: str, paused: bool) -> None:
        """Pause (hide) or unpause a ticket class."""
        self._client(token).request(
            "POST", f"/events/{remote_event_id}/ticket_classes/{remote_id}/", json={"ticket_class": {"hidden": paused}}
        )
