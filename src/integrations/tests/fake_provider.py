"""In-memory ``ListingProvider`` used by every non-translator test."""

import typing as t
from urllib.parse import urlencode

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
from integrations.schema import IntegrationErrorCode


class FakeProvider:
    key: t.ClassVar[str] = "fake"
    display_name: t.ClassVar[str] = "Fake"
    capabilities: t.ClassVar[Capabilities] = Capabilities(
        requires_end_time=True,
        requires_capacity=True,
        supports_structured_content=False,
        supports_unpublish_with_orders=True,
        single_currency_per_event=True,
    )

    def __init__(self, accounts: list[RemoteAccount] | None = None) -> None:
        self.accounts = accounts or [RemoteAccount(remote_id="acc-1", name="Fake Org")]
        self.exchanged: list[str] = []
        self.revoked: list[str] = []
        self.webhooks: dict[str, str] = {}  # remote webhook id -> url
        self.fail_exchange: ProviderError | None = None
        self.fail_webhook: ProviderError | None = None
        self._counter = 0
        self.events: dict[str, RemoteEvent] = {}
        self.calls: list[tuple[str, ...]] = []
        self.fail: dict[str, ProviderError] = {}
        self.missing: set[str] = set()
        self._event_counter = 0
        self._tc_counter = 0

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        return "https://fake.example/authorize?" + urlencode({"state": state, "redirect_uri": redirect_uri})

    def exchange_code(self, code: str, redirect_uri: str) -> TokenSet:
        if self.fail_exchange:
            raise self.fail_exchange
        self.exchanged.append(code)
        return TokenSet(access_token=f"tok-{code}")

    def revoke(self, token: TokenSet) -> None:
        self.revoked.append(token.access_token)

    def list_accounts(self, token: TokenSet) -> list[RemoteAccount]:
        if token.access_token == "tok-revoked":
            raise ProviderError(IntegrationErrorCode.CONNECTION_REVOKED, "401")
        return list(self.accounts)

    def register_webhook(self, token: TokenSet, account_id: str, url: str) -> str:
        if self.fail_webhook:
            raise self.fail_webhook
        self._counter += 1
        wid = f"wh-{self._counter}"
        self.webhooks[wid] = url
        return wid

    def unregister_webhook(self, token: TokenSet, remote_webhook_id: str) -> None:
        self.webhooks.pop(remote_webhook_id, None)

    def parse_webhook(self, request: HttpRequest) -> WebhookNotification:
        import orjson

        body: dict[str, t.Any] = orjson.loads(request.body or b"{}")
        if "action" not in body or "path" not in body:
            raise ProviderError(IntegrationErrorCode.PROVIDER_REJECTED, "malformed")
        return WebhookNotification(action=body["action"], resource_path=body["path"], raw=body)

    def _guard(self, method: str, *ids: str) -> None:
        """Record method call and check for configured failures."""
        self.calls.append((method, *ids))
        if method in self.fail:
            raise self.fail[method]

    def _stored(self, remote_id: str) -> RemoteEvent:
        """Retrieve a stored event or raise REMOTE_EVENT_MISSING."""
        if remote_id in self.missing or remote_id not in self.events:
            raise ProviderError(IntegrationErrorCode.REMOTE_EVENT_MISSING, "not found")
        return self.events[remote_id]

    # -- read -----------------------------------------------------------------
    def list_events(self, token: TokenSet, account_id: str) -> list[RemoteEventSummary]:
        """List remote events in an account."""
        self._guard("list_events", account_id)
        return [
            RemoteEventSummary(remote_id=rid, name=e.name, start=e.start, status=e.status, url=e.url)
            for rid, e in self.events.items()
        ]

    def get_event(self, token: TokenSet, remote_id: str) -> RemoteEvent:
        """Fetch a remote event including ticket classes and venue."""
        self._guard("get_event", remote_id)
        return self._stored(remote_id).model_copy(deep=True)

    # -- write ----------------------------------------------------------------
    def create_event(self, token: TokenSet, account_id: str, event: RemoteEvent) -> RemoteEventRef:
        """Create a remote event."""
        self._guard("create_event", account_id)
        self._event_counter += 1
        rid = f"ev-{self._event_counter}"
        url = f"https://fake.example/e/{rid}"
        self.events[rid] = event.model_copy(
            deep=True, update={"remote_id": rid, "status": "draft", "url": url, "ticket_classes": []}
        )
        return RemoteEventRef(remote_id=rid, url=url, status="draft")

    def update_event(self, token: TokenSet, remote_id: str, event: RemoteEvent) -> RemoteEventRef:
        """Update a remote event."""
        self._guard("update_event", remote_id)
        current = self._stored(remote_id)
        self.events[remote_id] = event.model_copy(
            deep=True,
            update={
                "remote_id": remote_id,
                "status": current.status,
                "url": current.url,
                "ticket_classes": current.ticket_classes,
            },
        )
        return RemoteEventRef(remote_id=remote_id, url=current.url, status=current.status)

    def set_description(self, token: TokenSet, remote_id: str, html: str) -> None:
        """Set long-form description (structured content on some platforms)."""
        self._guard("set_description", remote_id)
        self._stored(remote_id).description_html = html

    def publish_event(self, token: TokenSet, remote_id: str) -> None:
        """Publish a remote event."""
        self._guard("publish_event", remote_id)
        self._stored(remote_id).status = "live"

    def cancel_event(self, token: TokenSet, remote_id: str) -> None:
        """Cancel a remote event."""
        self._guard("cancel_event", remote_id)
        self._stored(remote_id).status = "cancelled"

    def upsert_ticket_class(self, token: TokenSet, remote_event_id: str, tc: RemoteTicketClass) -> str:
        """Create or update a ticket class, returning its remote ID."""
        self._guard("upsert_ticket_class", remote_event_id, tc.remote_id or "")
        event = self._stored(remote_event_id)
        if tc.remote_id is None:
            self._tc_counter += 1
            stored = tc.model_copy(update={"remote_id": f"tc-{self._tc_counter}"})
            event.ticket_classes.append(stored)
        else:
            if tc.remote_id not in {c.remote_id for c in event.ticket_classes}:
                raise ProviderError(IntegrationErrorCode.REMOTE_EVENT_MISSING, "ticket class not found")
            stored = tc.model_copy(deep=True)
            event.ticket_classes = [stored if c.remote_id == tc.remote_id else c for c in event.ticket_classes]
        return t.cast(str, stored.remote_id)

    def delete_ticket_class(self, token: TokenSet, remote_event_id: str, remote_id: str) -> None:
        """Delete a ticket class."""
        self._guard("delete_ticket_class", remote_event_id, remote_id)
        event = self._stored(remote_event_id)
        event.ticket_classes = [c for c in event.ticket_classes if c.remote_id != remote_id]

    def set_ticket_class_paused(self, token: TokenSet, remote_event_id: str, remote_id: str, paused: bool) -> None:
        """Pause (hide) or unpause a ticket class."""
        self._guard("set_ticket_class_paused", remote_event_id, remote_id)
        for c in self._stored(remote_event_id).ticket_classes:
            if c.remote_id == remote_id:
                c.hidden = paused
