"""Integrations API schemas and the stable error-code contract."""

import typing as t
from uuid import UUID

from ninja import Schema
from pydantic import AwareDatetime, Field

from integrations.enums import IntegrationErrorCode
from integrations.models import EventLink, ImportJob, PlatformConnection

__all__ = [
    "ConnectStartSchema",
    "ConnectionSchema",
    "ConnectionUpdateSchema",
    "EventLinkSchema",
    "EventLinkUpdateSchema",
    "EventListingSchema",
    "ImportJobSchema",
    "ImportRequestSchema",
    "ImportResultSchema",
    "IntegrationErrorCode",
    "IntegrationErrorSchema",
    "PauseRequestSchema",
    "PauseResultSchema",
    "RemoteAccountSchema",
    "RemoteEventSummarySchema",
    "SelectAccountSchema",
    "SyncReportEntry",
    "TierLinkSchema",
    "TierPauseFailureSchema",
]


class IntegrationErrorSchema(Schema):
    """Error response body carrying the stable code and optional provider context."""

    detail: str
    code: IntegrationErrorCode
    provider_message: str | None = None


class SyncReportEntry(Schema):
    """One problem or note from a sync run (spec §9). Stored as JSON on ``EventLink.sync_report``."""

    scope: t.Literal["event", "tier"]
    tier_id: UUID | None = None
    tier_name: str | None = None
    code: IntegrationErrorCode
    detail: str
    provider_message: str | None = None


class ConnectionSchema(Schema):
    """A platform connection and its authorization state."""

    provider: str
    display_name: str
    status: PlatformConnection.Status | None = None  # None = not connected
    remote_account_name: str = ""
    auto_sync: bool = False
    last_error: IntegrationErrorSchema | None = None
    connected_at: AwareDatetime | None = None
    stripe_connected: bool = False  # org-level: paid imported tiers start paused until this is True


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


class TierLinkSchema(Schema):
    """One ticket tier mirrored as a remote ticket class."""

    tier_id: UUID
    tier_name: str
    remote_id: str
    remote_quantity_sold: int
    counts_updated_at: AwareDatetime | None = None
    remote_paused: bool


class EventLinkSchema(Schema):
    """An event's link to one platform: sync state, report, and its tier links."""

    provider: str
    display_name: str
    remote_id: str
    remote_url: str
    remote_status: EventLink.RemoteStatus
    sync_state: EventLink.SyncState
    origin: EventLink.Origin
    auto_sync: bool | None
    effective_auto_sync: bool
    last_pushed_at: AwareDatetime | None = None
    last_pulled_at: AwareDatetime | None = None
    sync_report: list[SyncReportEntry]
    tiers: list[TierLinkSchema]


class EventListingSchema(Schema):
    """One row per enabled provider, so the event page can offer "list on X" before any link exists.

    Event staff cannot read the owner-only connection list, so the organization's connection
    state travels here; ``link`` is ``None`` until the event has been pushed or imported.
    """

    provider: str
    display_name: str
    connection_status: PlatformConnection.Status | None = None  # None = organization not connected
    link: EventLinkSchema | None = None


class EventLinkUpdateSchema(Schema):
    """Per-event auto-sync override (null = inherit the connection default)."""

    auto_sync: bool | None = None


class PauseRequestSchema(Schema):
    """Which tier to pause/resume; omitted or null means every linked tier."""

    tier_id: UUID | None = None


class TierPauseFailureSchema(Schema):
    """One tier the platform refused to pause or resume."""

    tier_id: UUID
    tier_name: str
    code: IntegrationErrorCode
    detail: str
    provider_message: str | None = None


class PauseResultSchema(Schema):
    """Outcome of a pause/resume request: which tiers changed, which failed, and the refreshed link."""

    paused: bool
    updated: list[UUID]
    failed: list[TierPauseFailureSchema]
    link: EventLinkSchema


class RemoteEventSummarySchema(Schema):
    """One remote event row for the import picker."""

    remote_id: str
    name: str
    start: AwareDatetime
    status: t.Literal["draft", "live", "cancelled"]
    url: str = ""
    already_linked: bool = False


class ImportRequestSchema(Schema):
    """Remote event ids to queue for import."""

    remote_ids: list[t.Annotated[str, Field(max_length=255)]] = Field(min_length=1, max_length=50)


class ImportJobSchema(Schema):
    """One queued import and its outcome; the picker polls these instead of the platform."""

    id: UUID
    remote_id: str
    status: ImportJob.Status
    event_id: UUID | None = None
    event_slug: str | None = None
    error_code: IntegrationErrorCode | None = None
    error_message: str = ""
    provider_message: str | None = None


class ImportResultSchema(Schema):
    """The jobs queued for import, and the remote ids skipped because they are already linked."""

    jobs: list[ImportJobSchema]
    skipped: list[str]
