"""Platform listing links: one connection per (organization, provider), one link per event."""

import secrets
import typing as t

from django.db import models
from encrypted_fields.fields import EncryptedTextField

from common.models import TimeStampedModel
from integrations.enums import IntegrationErrorCode
from integrations.providers.base import TokenSet


def new_webhook_secret() -> str:
    """Random URL-safe path token that hides the webhook endpoint from unregistered senders."""
    return secrets.token_urlsafe(32)


class ConnectionStatus(models.TextChoices):
    """A platform connection's authorization state.

    Named distinctly (not the generic ``Status``) so it gets its own OpenAPI component: several
    other models have an unrelated nested ``Status`` enum, and a shared bare name would collide
    in ``components.schemas`` (see ``api.management.commands.dump_openapi`` issue #782).
    """

    PENDING = "pending", "Pending account selection"
    ACTIVE = "active", "Active"
    REVOKED = "revoked", "Revoked"
    ERROR = "error", "Error"


class PlatformConnection(TimeStampedModel):
    """An organization's OAuth grant on one external platform (spec §5)."""

    class Provider(models.TextChoices):
        """Known provider keys, for reference and admin display.

        The column is deliberately unconstrained because ``registry.get_provider()`` validates
        keys at every entry point and tests register throwaway providers.
        """

        EVENTBRITE = "eventbrite", "Eventbrite"

    Status: t.TypeAlias = ConnectionStatus

    organization = models.ForeignKey(
        "events.Organization", on_delete=models.CASCADE, related_name="platform_connections"
    )
    provider = models.CharField(max_length=32)
    access_token = EncryptedTextField()
    refresh_token = EncryptedTextField(null=True, blank=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)
    remote_account_id = models.CharField(max_length=255, blank=True, default="")
    remote_account_name = models.CharField(max_length=255, blank=True, default="")
    auto_sync = models.BooleanField(
        default=False, help_text="Org-wide default; EventLink.auto_sync overrides per event."
    )
    webhook_remote_id = models.CharField(max_length=255, blank=True, default="")
    webhook_secret = models.CharField(max_length=64, unique=True, default=new_webhook_secret)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    last_error = models.JSONField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "provider"], name="unique_connection_per_provider")
        ]

    def __str__(self) -> str:
        return f"{self.provider} connection for {self.organization_id}"

    def token(self) -> TokenSet:
        """The stored credentials as the neutral ``TokenSet`` providers accept."""
        return TokenSet(
            access_token=self.access_token, refresh_token=self.refresh_token, expires_at=self.token_expires_at
        )

    def record_error(
        self,
        code: IntegrationErrorCode,
        message: str,
        provider_message: str | None = None,
        *,
        status: "PlatformConnection.Status | None" = None,
    ) -> None:
        """Persist a structured error (and optionally a status change) in one write."""
        self.last_error = {"code": code.value, "detail": message, "provider_message": provider_message}
        fields = ["last_error", "updated_at"]
        if status is not None:
            self.status = status
            fields.append("status")
        self.save(update_fields=fields)


class EventLink(TimeStampedModel):
    """A Revel event mirrored on one platform (spec §5)."""

    class RemoteStatus(models.TextChoices):
        DRAFT = "draft", "Draft"
        LIVE = "live", "Live"
        CANCELLED = "cancelled", "Cancelled"

    class SyncState(models.TextChoices):
        IN_SYNC = "in_sync", "In sync"
        PENDING = "pending", "Pending"
        FAILED = "failed", "Failed"
        BROKEN = "broken", "Broken (remote deleted)"

    class Origin(models.TextChoices):
        PUSHED = "pushed", "Pushed from Revel"
        IMPORTED = "imported", "Imported from platform"

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="platform_links")
    connection = models.ForeignKey(PlatformConnection, on_delete=models.CASCADE, related_name="event_links")
    remote_id = models.CharField(max_length=255, blank=True, default="")
    remote_url = models.URLField(blank=True, default="")
    auto_sync = models.BooleanField(null=True, blank=True, help_text="Null = inherit the connection default.")
    remote_status = models.CharField(max_length=16, choices=RemoteStatus.choices, default=RemoteStatus.DRAFT)
    sync_state = models.CharField(max_length=16, choices=SyncState.choices, default=SyncState.PENDING, db_index=True)
    origin = models.CharField(max_length=16, choices=Origin.choices, default=Origin.PUSHED)
    last_pushed_at = models.DateTimeField(null=True, blank=True)
    last_pulled_at = models.DateTimeField(null=True, blank=True)
    sync_report = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "connection"], name="unique_link_per_connection"),
            models.UniqueConstraint(
                fields=["connection", "remote_id"],
                condition=~models.Q(remote_id=""),
                name="unique_remote_id_per_connection",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.event_id} ↔ {self.connection_id}:{self.remote_id}"

    @property
    def effective_auto_sync(self) -> bool:
        """Per-event override when set, else the connection default."""
        return self.connection.auto_sync if self.auto_sync is None else self.auto_sync


class TierLink(TimeStampedModel):
    """A Revel ticket tier mirrored as one remote ticket class (spec §5)."""

    # SET_NULL, not CASCADE: a deleted Revel tier must leave its ``remote_id`` behind so the next
    # push can delete (or hide) the class it created remotely instead of orphaning it.
    tier = models.ForeignKey(
        "events.TicketTier", on_delete=models.SET_NULL, null=True, blank=True, related_name="platform_links"
    )
    event_link = models.ForeignKey(EventLink, on_delete=models.CASCADE, related_name="tier_links")
    remote_id = models.CharField(max_length=255)
    remote_quantity_sold = models.PositiveIntegerField(default=0)
    counts_updated_at = models.DateTimeField(null=True, blank=True)
    remote_paused = models.BooleanField(
        default=False, help_text="What Revel last set; the mapper reads this on every push."
    )
    last_error = models.JSONField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tier", "event_link"], name="unique_tier_link_per_event_link")]

    def __str__(self) -> str:
        return f"{self.tier_id} ↔ {self.remote_id}"


class WebhookDelivery(TimeStampedModel):
    """Audit row for every inbound delivery (spec §8). Idempotency is loose by design."""

    class Outcome(models.TextChoices):
        RECEIVED = "received", "Received"
        PROCESSED = "processed", "Processed"
        IGNORED = "ignored", "Ignored"
        FAILED = "failed", "Failed"

    connection = models.ForeignKey(PlatformConnection, on_delete=models.CASCADE, related_name="webhook_deliveries")
    action = models.CharField(max_length=100, db_index=True)
    resource_path = models.CharField(max_length=512, blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)
    outcome = models.CharField(max_length=16, choices=Outcome.choices, default=Outcome.RECEIVED, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.action} for connection {self.connection_id}"


class ImportJobStatus(models.TextChoices):
    """Lifecycle of one queued import. Module-level for its own OpenAPI component (see ``ConnectionStatus``)."""

    QUEUED = "queued", "Queued"
    DONE = "done", "Done"
    FAILED = "failed", "Failed"


class ImportJob(TimeStampedModel):
    """One queued "import this remote event" request the organizer can follow without polling the platform.

    ``done`` carries the resulting link; ``failed`` carries a stable ``IntegrationErrorCode`` plus the
    provider's own words.
    """

    Status: t.TypeAlias = ImportJobStatus

    connection = models.ForeignKey(PlatformConnection, on_delete=models.CASCADE, related_name="import_jobs")
    remote_id = models.CharField(max_length=255)
    status = models.CharField(max_length=16, choices=ImportJobStatus.choices, default=ImportJobStatus.QUEUED)
    link = models.ForeignKey(EventLink, on_delete=models.SET_NULL, null=True, blank=True, related_name="import_jobs")
    error_code = models.CharField(max_length=64, blank=True, default="")  # an IntegrationErrorCode value
    error_message = models.TextField(blank=True, default="")
    provider_message = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["connection", "remote_id"])]

    def __str__(self) -> str:
        return f"import {self.remote_id} ({self.status})"
