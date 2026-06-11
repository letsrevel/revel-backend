"""Read-only admin for the inbound Stripe webhook event log."""

from django.contrib import admin
from django.http import HttpRequest
from unfold.admin import ModelAdmin

from events import models


@admin.register(models.StripeWebhookEvent)
class StripeWebhookEventAdmin(ModelAdmin):  # type: ignore[misc]
    """Idempotency log of inbound Stripe events — fully read-only."""

    list_display = ["event_type", "event_id", "account", "outcome", "livemode", "created_at"]
    list_filter = ["event_type", "outcome", "livemode"]
    search_fields = ["event_id", "event_type", "account"]
    readonly_fields = [
        "id",
        "event_id",
        "event_type",
        "account",
        "livemode",
        "outcome",
        "payload",
        "created_at",
        "updated_at",
    ]
    date_hierarchy = "created_at"

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Rows are recorded by the webhook handler; humans don't add them."""
        return False

    def has_change_permission(self, request: HttpRequest, obj: models.StripeWebhookEvent | None = None) -> bool:
        """The log is an audit trail; rows are immutable once recorded."""
        return False

    def has_delete_permission(self, request: HttpRequest, obj: models.StripeWebhookEvent | None = None) -> bool:
        """Deleting the idempotency log would invite double-processing."""
        return False
