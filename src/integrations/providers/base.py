"""The provider seam: one Protocol, neutral shapes, no provider JSON above this line.

Spec §3.1–3.2. Phase 1 declares the connection and webhook half of the protocol; Phase 2
adds the read/write half. The read/write/count methods arrive with phase 2/3 and extend
this Protocol in place.
"""

import typing as t
from dataclasses import dataclass
from decimal import Decimal

from django.http import HttpRequest
from pydantic import AwareDatetime, BaseModel, Field


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


class RemoteVenue(BaseModel):
    """A remote platform's venue/location data."""

    name: str
    address: str = ""
    city: str = ""
    postal_code: str = ""
    country: str = ""  # ISO-3166 alpha-2 when known, else ""
    latitude: float | None = None
    longitude: float | None = None


class RemoteTicketClass(BaseModel):
    """A remote platform's ticket type."""

    remote_id: str | None = None
    name: str
    price: Decimal  # major units
    currency: str
    is_free: bool
    quantity_total: int
    quantity_sold: int = 0
    sales_start: AwareDatetime | None = None
    sales_end: AwareDatetime | None = None
    hidden: bool = False
    description: str = ""


RemoteStatus = t.Literal["draft", "live", "cancelled"]


class RemoteEvent(BaseModel):
    """A remote platform's event."""

    remote_id: str | None = None
    name: str
    summary: str = ""  # ≤ 140 chars, plain text
    description_html: str = ""
    start: AwareDatetime
    end: AwareDatetime
    timezone: str
    is_virtual: bool = False
    listed: bool = True  # discoverable in the platform's own search/listings
    venue: RemoteVenue | None = None
    currency: str
    status: RemoteStatus = "draft"
    url: str = ""
    ticket_classes: list[RemoteTicketClass] = Field(default_factory=list)


class RemoteEventSummary(BaseModel):
    """A remote platform's event summary (for list operations)."""

    remote_id: str
    name: str
    start: AwareDatetime
    status: RemoteStatus
    url: str = ""


class RemoteEventRef(BaseModel):
    """A remote platform's event reference (returned from create/update operations)."""

    remote_id: str
    url: str = ""
    status: RemoteStatus


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

    # -- read -----------------------------------------------------------------
    def list_events(self, token: TokenSet, account_id: str) -> list[RemoteEventSummary]:
        """List remote events in an account."""

    def get_event(self, token: TokenSet, remote_id: str) -> RemoteEvent:
        """Fetch a remote event including ticket classes and venue."""

    # -- write ----------------------------------------------------------------
    def create_event(self, token: TokenSet, account_id: str, event: RemoteEvent) -> RemoteEventRef:
        """Create a remote event."""

    def update_event(self, token: TokenSet, remote_id: str, event: RemoteEvent) -> RemoteEventRef:
        """Update a remote event."""

    def set_description(self, token: TokenSet, remote_id: str, html: str) -> None:
        """Set long-form description (structured content on some platforms)."""

    def publish_event(self, token: TokenSet, remote_id: str) -> None:
        """Publish a remote event."""

    def cancel_event(self, token: TokenSet, remote_id: str) -> None:
        """Cancel a remote event."""

    def upsert_ticket_class(self, token: TokenSet, remote_event_id: str, tc: RemoteTicketClass) -> str:
        """Create or update a ticket class, returning its remote ID."""

    def delete_ticket_class(self, token: TokenSet, remote_event_id: str, remote_id: str) -> None:
        """Delete a ticket class."""

    def set_ticket_class_paused(self, token: TokenSet, remote_event_id: str, remote_id: str, paused: bool) -> None:
        """Pause (hide) or unpause a ticket class."""
