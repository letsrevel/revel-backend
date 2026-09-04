"""Integrations API schemas and the stable error-code contract."""

from enum import StrEnum


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
