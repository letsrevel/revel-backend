# src/events/admin/event.py
"""Admin classes for Event and related models."""

import typing as t

from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin

from events import models
from events.admin.base import (
    EventInvitationInline,
    EventLinkMixin,
    EventRSVPInline,
    EventWaitListInline,
    OrganizationLinkMixin,
    TicketInline,
    TicketTierInline,
    UserLinkMixin,
)


@admin.register(models.Event)
class EventAdmin(ModelAdmin, OrganizationLinkMixin):  # type: ignore[misc]
    """Admin model for Events."""

    list_display = [
        "name",
        "organization_link",
        "event_type",
        "visibility",
        "status",
        "start",
        "attendee_count",
        "requires_ticket",
    ]
    list_filter = ["event_type", "organization", "start", "requires_ticket", "waitlist_open"]
    search_fields = ["name", "slug", "organization__name"]
    autocomplete_fields = ["organization", "event_series", "city", "venue"]
    prepopulated_fields = {"slug": ("name",)}
    date_hierarchy = "start"

    tabs = [
        ("Details", ["Details"]),
        ("Attendees", ["RSVPs", "Tickets", "Invitations", "Waitlist"]),
        ("Configuration", ["Tiers & Resources"]),
    ]

    fieldsets = [
        (
            "Details",
            {
                "fields": (
                    "organization",
                    ("name", "slug"),
                    "venue",
                    ("city", "address"),
                    "description",
                    "invitation_message",
                    ("logo", "cover_art"),
                )
            },
        ),
        (
            "Configuration",
            {
                "fields": (
                    ("event_type", "visibility", "status"),
                    ("start", "end"),
                    "event_series",
                    ("max_attendees",),
                    (
                        "waitlist_open",
                        "potluck_open",
                        "accept_invitation_requests",
                        "can_attend_without_login",
                    ),
                    "requires_ticket",
                    ("check_in_starts_at", "check_in_ends_at"),
                )
            },
        ),
    ]

    inlines = [EventRSVPInline, TicketInline, EventInvitationInline, EventWaitListInline, TicketTierInline]

    @admin.display(description="Attendees")
    def attendee_count(self, obj: models.Event) -> str:
        if obj.requires_ticket:
            count = obj.tickets.count()
        else:
            count = obj.rsvps.filter(status=models.EventRSVP.RsvpStatus.YES).count()

        limit = obj.max_attendees if obj.max_attendees > 0 else "∞"
        return f"{count} / {limit}"


@admin.register(models.EventSeries)
class EventSeriesAdmin(ModelAdmin, OrganizationLinkMixin):  # type: ignore[misc]
    list_display = ["name", "slug", "organization_link"]
    search_fields = ["name", "slug", "organization__name"]
    autocomplete_fields = ["organization"]
    prepopulated_fields = {"slug": ("name",)}


@admin.register(models.EventInvitation)
class EventInvitationAdmin(ModelAdmin, UserLinkMixin, EventLinkMixin):  # type: ignore[misc]
    list_display = ["__str__", "user_link", "event_link", "tier_name", "waives_questionnaire", "waives_purchase"]
    list_filter = ["event__name", "tier__name"]
    search_fields = ["user__username", "event__name"]
    autocomplete_fields = ["user", "event", "tier"]

    @admin.display(description="Tier")
    def tier_name(self, obj: models.EventInvitation) -> str | None:
        return obj.tier.name if obj.tier else "—"


@admin.register(models.EventRSVP)
class EventRSVPAdmin(ModelAdmin, UserLinkMixin, EventLinkMixin):  # type: ignore[misc]
    list_display = ["__str__", "user_link", "event_link", "status"]
    list_filter = ["status", "event__name"]
    search_fields = ["user__username", "event__name"]
    autocomplete_fields = ["user", "event"]


@admin.register(models.PendingEventInvitation)
class PendingEventInvitationAdmin(ModelAdmin, EventLinkMixin):  # type: ignore[misc]
    """Admin for PendingEventInvitation model."""

    list_display = ["email", "event_link", "tier_name", "waives_questionnaire", "waives_purchase", "created_at"]
    list_filter = ["event__organization", "tier__name", "waives_questionnaire", "waives_purchase", "created_at"]
    search_fields = ["email", "event__name", "tier__name"]
    autocomplete_fields = ["event", "tier"]
    readonly_fields = ["created_at", "updated_at"]
    date_hierarchy = "created_at"

    @admin.display(description="Tier")
    def tier_name(self, obj: models.PendingEventInvitation) -> str | None:
        return obj.tier.name if obj.tier else "—"


@admin.register(models.EventToken)
class EventTokenAdmin(ModelAdmin, UserLinkMixin, EventLinkMixin):  # type: ignore[misc]
    """Admin for EventToken model."""

    list_display = ["__str__", "name", "event_link", "issuer_link", "uses_display", "expires_at", "created_at"]
    list_filter = ["event__organization", "expires_at", "created_at"]
    search_fields = ["name", "event__name", "issuer__username"]
    autocomplete_fields = ["event", "issuer", "ticket_tier"]
    readonly_fields = ["id", "created_at", "updated_at"]
    date_hierarchy = "created_at"

    @admin.display(description="Issuer")
    def issuer_link(self, obj: models.EventToken) -> str:
        url = reverse("admin:accounts_reveluser_change", args=[obj.issuer.id])
        return format_html('<a href="{}">{}</a>', url, obj.issuer.username)

    @admin.display(description="Uses")
    def uses_display(self, obj: models.EventToken) -> str:
        if obj.max_uses > 0:
            return f"{obj.uses} / {obj.max_uses}"
        return f"{obj.uses} / ∞"


@admin.register(models.EventInvitationRequest)
class EventInvitationRequestAdmin(ModelAdmin, UserLinkMixin, EventLinkMixin):  # type: ignore[misc]
    """Admin for EventInvitationRequest model."""

    list_display = ["__str__", "user_link", "event_link", "status_display", "decided_by_link", "created_at"]
    list_filter = ["status", "event__organization", "created_at"]
    search_fields = ["user__username", "user__email", "event__name", "message"]
    autocomplete_fields = ["user", "event", "decided_by"]
    readonly_fields = ["created_at", "updated_at"]
    date_hierarchy = "created_at"

    @admin.display(description="Status")
    def status_display(self, obj: models.EventInvitationRequest) -> str:
        colors: dict[t.Any, str] = {
            models.EventInvitationRequest.InvitationRequestStatus.PENDING: "orange",
            models.EventInvitationRequest.InvitationRequestStatus.APPROVED: "green",
            models.EventInvitationRequest.InvitationRequestStatus.REJECTED: "red",
        }
        color = colors.get(obj.status, "gray")
        return mark_safe(f'<span style="color: {color};">{obj.get_status_display()}</span>')

    @admin.display(description="Decided By")
    def decided_by_link(self, obj: models.EventInvitationRequest) -> str | None:
        if not obj.decided_by:
            return "—"
        url = reverse("admin:accounts_reveluser_change", args=[obj.decided_by.id])
        return format_html('<a href="{}">{}</a>', url, obj.decided_by.username)


@admin.register(models.EventWaitList)
class EventWaitListAdmin(ModelAdmin, UserLinkMixin, EventLinkMixin):  # type: ignore[misc]
    """Admin for EventWaitList model."""

    list_display = ["__str__", "user_link", "event_link", "position_display", "created_at"]
    list_filter = ["event__organization", "event__name", "created_at"]
    search_fields = ["user__username", "user__email", "event__name"]
    autocomplete_fields = ["user", "event"]
    readonly_fields = ["created_at", "updated_at", "position_display"]
    date_hierarchy = "created_at"
    ordering = ["event", "created_at"]

    def position_display(self, obj: models.EventWaitList) -> int:
        # Calculate position in waitlist
        earlier_entries = models.EventWaitList.objects.filter(event=obj.event, created_at__lt=obj.created_at).count()
        return earlier_entries + 1

    position_display.short_description = "Position"  # type: ignore[attr-defined]


@admin.register(models.PotluckItem)
class PotluckItemAdmin(ModelAdmin, EventLinkMixin):  # type: ignore[misc]
    """Admin for PotluckItem model."""

    list_display = [
        "name",
        "event_link",
        "assignee_link",
        "created_at",
    ]
    list_filter = ["event__organization", "event__name", "created_at"]
    search_fields = ["name", "event__name", "assignee__username"]
    autocomplete_fields = ["event", "assignee"]
    readonly_fields = ["created_at", "updated_at"]
    date_hierarchy = "created_at"

    @admin.display(description="Assignee")
    def assignee_link(self, obj: models.PotluckItem) -> str | None:
        if not obj.assignee:
            return "Unassigned"
        url = reverse("admin:accounts_reveluser_change", args=[obj.assignee.id])
        return format_html('<a href="{}">{}</a>', url, obj.assignee.username)


@admin.register(models.AdditionalResource)
class EventResourceAdmin(ModelAdmin):  # type: ignore[misc]
    list_display = ["__str__", "name", "resource_type"]
    list_filter = ["resource_type"]
    search_fields = ["name", "description"]
    autocomplete_fields = ["organization", "events", "event_series"]
    filter_horizontal = ["events"]
