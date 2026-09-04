"""Admin classes for integrations models."""

from django.contrib import admin
from unfold.admin import ModelAdmin

from integrations.models import EventLink, PlatformConnection, TierLink, WebhookDelivery


@admin.register(PlatformConnection)
class PlatformConnectionAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for PlatformConnection model."""

    list_display = ("organization", "provider", "status", "remote_account_name", "auto_sync", "updated_at")
    list_filter = ("provider", "status")
    exclude = ("access_token", "refresh_token", "webhook_secret")
    readonly_fields = ("last_error",)


@admin.register(EventLink)
class EventLinkAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for EventLink model."""

    list_display = ("event", "connection", "remote_id", "remote_status", "sync_state", "origin", "last_pushed_at")
    list_filter = ("remote_status", "sync_state", "origin")


@admin.register(TierLink)
class TierLinkAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for TierLink model."""

    list_display = ("tier", "event_link", "remote_id", "remote_quantity_sold", "remote_paused")
    list_filter = ("remote_paused",)


@admin.register(WebhookDelivery)
class WebhookDeliveryAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for WebhookDelivery model."""

    list_display = ("connection", "action", "outcome", "created_at")
    list_filter = ("action", "outcome")
