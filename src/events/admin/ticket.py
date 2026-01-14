# src/events/admin/ticket.py
"""Admin classes for Ticket, TicketTier, and Payment models."""

import typing as t

from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin

from events import models
from events.admin.base import (
    EventLinkMixin,
    UserLinkMixin,
    VenueLinkMixin,
)


@admin.register(models.TicketTier)
class TicketTierAdmin(ModelAdmin, EventLinkMixin, VenueLinkMixin):  # type: ignore[misc]
    """Admin view for TicketTier."""

    list_display = ["__str__", "name", "event_link", "venue_link", "sector_name", "short_description"]
    list_filter = ["event", "venue", "sector"]
    search_fields = ["name", "event__name", "description"]
    autocomplete_fields = ["event", "venue", "sector"]
    filter_horizontal = ["restricted_to_membership_tiers"]

    def short_description(self, obj: models.TicketTier) -> str:
        return obj.description[:100] if obj.description else "-"

    short_description.short_description = "Description"  # type: ignore[attr-defined]

    @admin.display(description="Sector")
    def sector_name(self, obj: models.TicketTier) -> str:
        return obj.sector.name if obj.sector else "—"


@admin.register(models.Ticket)
class TicketAdmin(ModelAdmin, UserLinkMixin, EventLinkMixin, VenueLinkMixin):  # type: ignore[misc]
    list_display = [
        "id",
        "event_link",
        "user_link",
        "tier_name",
        "venue_link",
        "sector_name",
        "seat_label",
        "status",
        "checked_in_at",
    ]
    list_filter = ["status", "event__name", "tier__name", "venue", "sector"]
    search_fields = ["event__name", "user__username", "seat__label"]
    autocomplete_fields = ["event", "user", "tier", "checked_in_by", "venue", "sector", "seat"]
    readonly_fields = ["id", "checked_in_at", "checked_in_by"]
    date_hierarchy = "created_at"

    @admin.display(description="Tier")
    def tier_name(self, obj: models.Ticket) -> str | None:
        return obj.tier.name if obj.tier else "—"

    @admin.display(description="Sector")
    def sector_name(self, obj: models.Ticket) -> str | None:
        return obj.sector.name if obj.sector else "—"

    @admin.display(description="Seat")
    def seat_label(self, obj: models.Ticket) -> str | None:
        return obj.seat.label if obj.seat else "—"


@admin.register(models.Payment)
class PaymentAdmin(ModelAdmin, UserLinkMixin, EventLinkMixin):  # type: ignore[misc]
    """Admin for Payment model with financial tracking."""

    list_display = [
        "id_short",
        "user_link",
        "ticket_link",
        "amount_display",
        "status_display",
        "stripe_session_id_short",
        "expires_at",
        "created_at",
    ]
    list_filter = ["status", "currency", "created_at", "expires_at"]
    search_fields = ["user__username", "user__email", "ticket__event__name", "stripe_session_id"]
    readonly_fields = ["id", "user", "ticket", "stripe_session_id", "raw_response", "created_at", "updated_at"]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]

    @admin.display(description="ID")
    def id_short(self, obj: models.Payment) -> str:
        return str(obj.id)[:8] + "..."

    @admin.display(description="Ticket")
    def ticket_link(self, obj: models.Payment) -> str:
        url = reverse("admin:events_ticket_change", args=[obj.ticket.id])
        return format_html('<a href="{}">{}</a>', url, f"Ticket {str(obj.ticket.id)[:8]}...")

    @admin.display(description="Amount")
    def amount_display(self, obj: models.Payment) -> str:
        return f"{obj.amount} {obj.currency}"

    @admin.display(description="Status")
    def status_display(self, obj: models.Payment) -> str:
        colors: dict[t.Any, str] = {
            models.Payment.PaymentStatus.PENDING: "orange",
            models.Payment.PaymentStatus.SUCCEEDED: "green",
            models.Payment.PaymentStatus.FAILED: "red",
            models.Payment.PaymentStatus.REFUNDED: "blue",
        }
        color = colors.get(obj.status, "gray")
        return mark_safe(f'<span style="color: {color};">{obj.get_status_display()}</span>')

    @admin.display(description="Stripe Session")
    def stripe_session_id_short(self, obj: models.Payment) -> str:
        if obj.stripe_session_id:
            return obj.stripe_session_id[:20] + "..."
        return "-"
