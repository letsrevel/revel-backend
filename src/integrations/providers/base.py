"""The provider seam: one Protocol, neutral shapes, no provider JSON above this line.

Spec §3.1–3.2. Phase 1 declares the connection and webhook half of the protocol; the
read/write/count methods arrive with phase 2/3 and extend this Protocol in place.
"""

import typing as t
from dataclasses import dataclass

from django.http import HttpRequest
from pydantic import AwareDatetime, BaseModel


@dataclass(frozen=True)
class Capabilities:
    """What a platform cannot do, so the shared mapper warns instead of crashing."""

    requires_end_time: bool
    requires_capacity: bool
    supports_structured_content: bool
    supports_unpublish_with_orders: bool
    single_currency_per_event: bool


class TokenSet(BaseModel):
    access_token: str
    refresh_token: str | None = None
    expires_at: AwareDatetime | None = None


class RemoteAccount(BaseModel):
    remote_id: str
    name: str


class WebhookNotification(BaseModel):
    """A parsed inbound delivery: action + the resource *path* (host stripped — spec §8)."""

    action: str
    resource_path: str
    raw: dict[str, t.Any]


@t.runtime_checkable
class ListingProvider(t.Protocol):
    """Everything the shared layer needs from a platform. Methods raise ``ProviderError``."""

    key: t.ClassVar[str]
    display_name: t.ClassVar[str]
    capabilities: t.ClassVar[Capabilities]

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        """Generate an authorization URL for OAuth flow."""

    def exchange_code(self, code: str, redirect_uri: str) -> TokenSet:
        """Exchange an authorization code for a token set."""

    def revoke(self, token: TokenSet) -> None:
        """Revoke a token set."""

    def list_accounts(self, token: TokenSet) -> list[RemoteAccount]:
        """List remote accounts accessible with the given token."""

    def register_webhook(self, token: TokenSet, account_id: str, url: str) -> str:
        """Register a webhook and return its remote ID."""

    def unregister_webhook(self, token: TokenSet, remote_webhook_id: str) -> None:
        """Unregister a webhook by its remote ID."""

    def parse_webhook(self, request: HttpRequest) -> WebhookNotification:
        """Parse an inbound webhook request."""
