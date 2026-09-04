"""Admin classes for integrations models."""

import typing as t

from django.contrib import admin
from django.http import HttpRequest
from unfold.admin import ModelAdmin

from integrations.models import EventLink, PlatformConnection, TierLink, WebhookDelivery


@admin.register(PlatformConnection)
class PlatformConnectionAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for PlatformConnection model. Read-only: connections are managed via the OAuth flow."""

    list_display = ("organization", "provider", "status", "remote_account_name", "auto_sync", "updated_at")
    list_filter = ("provider", "status")
    exclude = ("access_token", "refresh_token", "webhook_secret")
    readonly_fields = ("last_error",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Connections are created via the OAuth callback, never in the admin."""
        return False

    def has_change_permission(self, request: HttpRequest, obj: t.Any = None) -> bool:
        """Connections are managed via the OAuth flow, not hand-edited."""
        return False

    def has_delete_permission(self, request: HttpRequest, obj: t.Any = None) -> bool:
        """Connections are removed via the disconnect flow, never in the admin."""
        return False


@admin.register(EventLink)
class EventLinkAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for EventLink model. Read-only: links are managed by the sync service."""

    list_display = ("event", "connection", "remote_id", "remote_status", "sync_state", "origin", "last_pushed_at")
    list_filter = ("remote_status", "sync_state", "origin")

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Links are created by the sync service, never in the admin."""
        return False

    def has_change_permission(self, request: HttpRequest, obj: t.Any = None) -> bool:
        """Links are managed by the sync service, not hand-edited."""
        return False

    def has_delete_permission(self, request: HttpRequest, obj: t.Any = None) -> bool:
        """Links are removed by the sync service, never in the admin."""
        return False


@admin.register(TierLink)
class TierLinkAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for TierLink model. Read-only: links are managed by the sync service."""

    list_display = ("tier", "event_link", "remote_id", "remote_quantity_sold", "remote_paused")
    list_filter = ("remote_paused",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Links are created by the sync service, never in the admin."""
        return False

    def has_change_permission(self, request: HttpRequest, obj: t.Any = None) -> bool:
        """Links are managed by the sync service, not hand-edited."""
        return False

    def has_delete_permission(self, request: HttpRequest, obj: t.Any = None) -> bool:
        """Links are removed by the sync service, never in the admin."""
        return False


@admin.register(WebhookDelivery)
class WebhookDeliveryAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for WebhookDelivery model. Read-only: an audit trail of inbound provider deliveries."""

    list_display = ("connection", "action", "outcome", "created_at")
    list_filter = ("action", "outcome")

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Rows are recorded by the webhook handler, never in the admin."""
        return False

    def has_change_permission(self, request: HttpRequest, obj: t.Any = None) -> bool:
        """The log is an audit trail; rows are immutable once recorded."""
        return False

    def has_delete_permission(self, request: HttpRequest, obj: t.Any = None) -> bool:
        """Deleting the delivery log would invite double-processing."""
        return False
