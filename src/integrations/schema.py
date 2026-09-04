"""Integrations API schemas and the stable error-code contract."""

import typing as t
from enum import StrEnum

from ninja import Schema
from pydantic import AwareDatetime


class IntegrationErrorCode(StrEnum):
    """Machine-readable failure codes. Exposed via OpenAPI; the frontend owns the copy per code.

    Spec §9. Keep values stable — they are a contract with the frontend.
    """

    PROVIDER_UNKNOWN = "provider_unknown"
    PROVIDER_NOT_CONNECTED = "provider_not_connected"
    ALREADY_CONNECTED = "already_connected"
    CONNECTION_PENDING = "connection_pending"
    CONNECTION_REVOKED = "connection_revoked"
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    PROVIDER_REJECTED = "provider_rejected"
    STATE_INVALID = "state_invalid"
    ACCOUNT_UNKNOWN = "account_unknown"
    WEBHOOK_REGISTRATION_FAILED = "webhook_registration_failed"
    # Sync-time codes (spec §9) — declared now so the contract is complete; used from phase 2.
    EVENT_PRIVATE = "event_private"
    EVENT_OPEN_ENDED = "event_open_ended"
    EVENT_NO_TICKETS = "event_no_tickets"
    TIER_VARIABLE_PRICE = "tier_variable_price"
    TIER_MEMBERS_ONLY = "tier_members_only"
    TIER_SEATED = "tier_seated"
    TIER_OFFLINE_PAYMENT = "tier_offline_payment"
    TIER_NO_CAPACITY = "tier_no_capacity"
    TIER_CURRENCY_MISMATCH = "tier_currency_mismatch"
    REMOTE_EVENT_MISSING = "remote_event_missing"
    UNPUBLISH_REFUSED = "unpublish_refused"
    IMAGE_MISSING = "image_missing"
    PAUSE_FAILED = "pause_failed"


class IntegrationErrorSchema(Schema):
    """Error response body carrying the stable code and optional provider context."""

    detail: str
    code: IntegrationErrorCode
    provider_message: str | None = None


class ProviderSchema(Schema):
    """Provider descriptor for the provider list endpoint."""

    key: str
    display_name: str


ConnectionStatus = t.Literal["pending", "active", "revoked", "error"]


class ConnectionSchema(Schema):
    """A platform connection and its authorization state."""

    provider: str
    display_name: str
    status: ConnectionStatus | None = None  # None = not connected
    remote_account_name: str = ""
    auto_sync: bool = False
    last_error: IntegrationErrorSchema | None = None
    connected_at: AwareDatetime | None = None


class ConnectStartSchema(Schema):
    """OAuth authorization URL for initiating a platform connection."""

    authorize_url: str


class RemoteAccountSchema(Schema):
    """A remote account discovered during OAuth authorization."""

    remote_id: str
    name: str


class SelectAccountSchema(Schema):
    """Request body to confirm a remote account selection and persist the connection."""

    remote_id: str


class ConnectionUpdateSchema(Schema):
    """Update request body for connection settings like auto-sync preference."""

    auto_sync: bool
