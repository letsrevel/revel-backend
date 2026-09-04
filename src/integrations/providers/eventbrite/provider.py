"""Eventbrite implementation of ``ListingProvider``."""

import typing as t

from django.http import HttpRequest

from integrations.providers.base import Capabilities, RemoteAccount, TokenSet, WebhookNotification


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

    def __init__(self, client_id: str, client_secret: str) -> None:
        """Initialize with Eventbrite OAuth credentials."""
        self.client_id = client_id
        self.client_secret = client_secret

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        """Generate an authorization URL for OAuth flow."""
        raise NotImplementedError()

    def exchange_code(self, code: str, redirect_uri: str) -> TokenSet:
        """Exchange an authorization code for a token set."""
        raise NotImplementedError()

    def revoke(self, token: TokenSet) -> None:
        """Revoke a token set."""
        raise NotImplementedError()

    def list_accounts(self, token: TokenSet) -> list[RemoteAccount]:
        """List remote accounts accessible with the given token."""
        raise NotImplementedError()

    def register_webhook(self, token: TokenSet, account_id: str, url: str) -> str:
        """Register a webhook and return its remote ID."""
        raise NotImplementedError()

    def unregister_webhook(self, token: TokenSet, remote_webhook_id: str) -> None:
        """Unregister a webhook by its remote ID."""
        raise NotImplementedError()

    def parse_webhook(self, request: HttpRequest) -> WebhookNotification:
        """Parse an inbound webhook request."""
        raise NotImplementedError()
