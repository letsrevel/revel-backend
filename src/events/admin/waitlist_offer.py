"""Django admin for WaitlistOffer."""

from django.contrib import admin
from unfold.admin import ModelAdmin

from events import models

from .base import EventLinkMixin, UserLinkMixin


@admin.register(models.WaitlistOffer)
class WaitlistOfferAdmin(ModelAdmin, UserLinkMixin, EventLinkMixin):  # type: ignore[misc]
    """Admin for WaitlistOffer model."""

    list_display = [
        "id",
        "event_link",
        "user_link",
        "status",
        "expires_at",
        "is_cutoff_batch",
        "batch_id",
        "created_at",
    ]
    list_filter = ["status", "is_cutoff_batch"]
    search_fields = ["user__email", "event__name", "batch_id"]
    readonly_fields = ["created_at", "updated_at", "notified_at", "claimed_at", "batch_id"]
    ordering = ["-created_at"]
