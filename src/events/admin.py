# src/events/admin.py

import typing as t

from django import forms
from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin, TabularInline
from unfold.widgets import CHECKBOX_CLASSES

from . import models


# --- Helper Mixins for Reusable Link Fields ---
class UserLinkMixin:
    """Mixin to add a link to a user."""

    def user_link(self, obj: t.Any) -> str:
        user = getattr(obj, "user", getattr(obj, "owner", None))
        url = reverse("admin:accounts_reveluser_change", args=[user.id])  # type: ignore[union-attr]
        return format_html('<a href="{}">{}</a>', url, user.username)  # type: ignore[union-attr]

    user_link.short_description = "User"  # type: ignore[attr-defined]


class OrganizationLinkMixin:
    """Mixin to add a link to an organization."""

    def organization_link(self, obj: t.Any) -> str | None:
        if not hasattr(obj, "organization") or not obj.organization:
            return None
        url = reverse("admin:events_organization_change", args=[obj.organization.id])
        return format_html('<a href="{}">{}</a>', url, obj.organization.name)

    organization_link.short_description = "Organization"  # type: ignore[attr-defined]


class EventLinkMixin:
    """Mixin to add a link to an event."""

    def event_link(self, obj: t.Any) -> str | None:
        if not hasattr(obj, "event") or not obj.event:
            return None
        url = reverse("admin:events_event_change", args=[obj.event.id])
        return format_html('<a href="{}">{}</a>', url, obj.event.name)

    event_link.short_description = "Event"  # type: ignore[attr-defined]


class VenueLinkMixin:
    """Mixin to add a link to a venue."""

    def venue_link(self, obj: t.Any) -> str | None:
        if not hasattr(obj, "venue") or not obj.venue:
            return None
        url = reverse("admin:events_venue_change", args=[obj.venue.id])
        return format_html('<a href="{}">{}</a>', url, obj.venue.name)

    venue_link.short_description = "Venue"  # type: ignore[attr-defined]


# --- Custom Forms ---
class OrganizationStaffForm(forms.ModelForm):  # type: ignore[type-arg]
    """Custom form to manage the 'permissions' JSONField for OrganizationStaff."""

    # Dynamically create form fields for each default permission
    for key in models.PermissionMap.model_fields:
        locals()[f"default_{key}"] = forms.BooleanField(
            label=key.replace("_", " ").title(),
            required=False,
            widget=forms.CheckboxInput(attrs={"class": CHECKBOX_CLASSES}),
        )

    class Meta:
        model = models.OrganizationStaff
        fields = "__all__"

    def __init__(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Overriding init."""
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            # Populate form fields from the instance's permissions JSON
            permissions = self.instance.permissions or {}
            default_perms = permissions.get("default", {})
            for key in models.PermissionMap.model_fields:
                self.fields[f"default_{key}"].initial = default_perms.get(key, False)

    def save(self, commit: bool = True) -> models.OrganizationStaff:
        # Reconstruct the 'permissions' dictionary before saving
        default_perms = {
            key: self.cleaned_data.get(f"default_{key}", False) for key in models.PermissionMap.model_fields
        }
        # Keep existing event_overrides, as we don't manage them in this form
        current_overrides = (self.instance.permissions or {}).get("event_overrides", {})

        self.instance.permissions = {
            "default": default_perms,
            "event_overrides": current_overrides,
        }
        return t.cast(models.OrganizationStaff, super().save(commit))


# --- Inlines ---
class OrganizationStaffInline(TabularInline):  # type: ignore[misc]
    model = models.OrganizationStaff
    form = OrganizationStaffForm
    extra = 1
    autocomplete_fields = ["user"]
    fields = ["user"] + [f"default_{key}" for key in models.PermissionMap.model_fields]


class OrganizationMemberInline(TabularInline):  # type: ignore[misc]
    model = models.OrganizationMember
    extra = 1
    autocomplete_fields = ["user", "tier"]
    fields = ["user", "status", "tier"]


class EventSeriesInline(TabularInline):  # type: ignore[misc]
    model = models.EventSeries
    extra = 1
    show_change_link = True
    fields = ["name", "slug"]
    prepopulated_fields = {"slug": ("name",)}


class OrganizationQuestionnaireInline(TabularInline):  # type: ignore[misc]
    model = models.OrganizationQuestionnaire
    extra = 1
    autocomplete_fields = ["questionnaire"]
    filter_horizontal = ["events", "event_series"]


class MembershipTierInline(TabularInline):  # type: ignore[misc]
    model = models.MembershipTier
    extra = 1
    fields = ["name", "description"]


class VenueInline(TabularInline):  # type: ignore[misc]
    model = models.Venue
    extra = 0
    show_change_link = True
    fields = ["name", "slug", "capacity"]
    prepopulated_fields = {"slug": ("name",)}


class VenueSectorInline(TabularInline):  # type: ignore[misc]
    model = models.VenueSector
    extra = 1
    fields = ["name", "code", "capacity", "display_order"]
    ordering = ["display_order", "name"]


class VenueSeatInline(TabularInline):  # type: ignore[misc]
    model = models.VenueSeat
    extra = 1
    fields = ["label", "row", "number", "is_accessible", "is_obstructed_view", "is_active"]
    ordering = ["row", "number", "label"]


class TicketTierInline(TabularInline):  # type: ignore[misc]
    model = models.TicketTier
    extra = 1


class EventInvitationInline(TabularInline):  # type: ignore[misc]
    model = models.EventInvitation
    extra = 1
    autocomplete_fields = ["user", "tier"]


class EventRSVPInline(TabularInline):  # type: ignore[misc]
    model = models.EventRSVP
    extra = 1
    autocomplete_fields = ["user"]


class TicketInline(TabularInline):  # type: ignore[misc]
    model = models.Ticket
    extra = 0
    autocomplete_fields = ["user", "tier", "checked_in_by"]
    readonly_fields = ["id", "checked_in_at"]
    can_delete = False


class EventWaitListInline(TabularInline):  # type: ignore[misc]
    model = models.EventWaitList
    extra = 0
    autocomplete_fields = ["user"]
    readonly_fields = ["created_at"]


# --- ModelAdmins ---
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


@admin.register(models.Organization)
class OrganizationAdmin(ModelAdmin, UserLinkMixin):  # type: ignore[misc]
    """Admin model for Organizations."""

    list_display = ["name", "slug", "owner_link", "visibility", "created_at"]
    search_fields = ["name", "slug", "owner__username"]
    autocomplete_fields = ["owner", "city", "staff_members", "members"]
    prepopulated_fields = {"slug": ("name",)}

    tabs = [
        ("Settings", ["Settings"]),
        ("People", ["Staff", "Members", "Tiers"]),
        ("Content", ["Series", "Questionnaires", "Venues"]),
    ]

    inlines = [
        OrganizationStaffInline,
        OrganizationMemberInline,
        MembershipTierInline,
        EventSeriesInline,
        OrganizationQuestionnaireInline,
        VenueInline,
    ]

    def owner_link(self, obj: models.Organization) -> str:
        return self.user_link(obj)

    owner_link.short_description = "Owner"  # type: ignore[attr-defined]


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


# --- Venue Model Admins ---


@admin.register(models.Venue)
class VenueAdmin(ModelAdmin, OrganizationLinkMixin):  # type: ignore[misc]
    """Admin for Venue model."""

    list_display = ["name", "slug", "organization_link", "capacity", "city_name"]
    list_filter = ["organization__name", "city__country"]
    search_fields = ["name", "slug", "organization__name", "address"]
    autocomplete_fields = ["organization", "city"]
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ["created_at", "updated_at"]

    fieldsets = [
        (
            "Basic Information",
            {
                "fields": (
                    "organization",
                    ("name", "slug"),
                    "description",
                    "capacity",
                )
            },
        ),
        (
            "Location",
            {
                "fields": (
                    "city",
                    "address",
                    "location",
                )
            },
        ),
        (
            "Metadata",
            {"fields": (("created_at", "updated_at"),)},
        ),
    ]

    inlines = [VenueSectorInline]

    @admin.display(description="City")
    def city_name(self, obj: models.Venue) -> str:
        return str(obj.city) if obj.city else "—"


@admin.register(models.VenueSector)
class VenueSectorAdmin(ModelAdmin, VenueLinkMixin):  # type: ignore[misc]
    """Admin for VenueSector model."""

    list_display = ["name", "venue_link", "code", "capacity", "display_order"]
    list_filter = ["venue__organization__name"]
    search_fields = ["name", "code", "venue__name"]
    autocomplete_fields = ["venue"]
    readonly_fields = ["created_at", "updated_at"]
    ordering = ["venue", "display_order", "name"]

    fieldsets = [
        (
            "Basic Information",
            {
                "fields": (
                    "venue",
                    ("name", "code"),
                    "capacity",
                )
            },
        ),
        (
            "Display Configuration",
            {
                "fields": (
                    "metadata",
                    "display_order",
                    "shape",
                )
            },
        ),
        (
            "Metadata",
            {"fields": (("created_at", "updated_at"),)},
        ),
    ]

    inlines = [VenueSeatInline]


@admin.register(models.VenueSeat)
class VenueSeatAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for VenueSeat model."""

    list_display = ["label", "sector_link", "row", "number", "is_accessible", "is_obstructed_view", "is_active"]
    list_filter = ["sector__venue__organization__name", "is_accessible", "is_obstructed_view", "is_active"]
    search_fields = ["label", "row", "sector__name", "sector__venue__name"]
    autocomplete_fields = ["sector"]
    readonly_fields = ["created_at", "updated_at"]
    ordering = ["sector", "row", "number", "label"]

    fieldsets = [
        (
            "Basic Information",
            {
                "fields": (
                    "sector",
                    "label",
                    ("row", "number"),
                )
            },
        ),
        (
            "Position & Properties",
            {
                "fields": (
                    "position",
                    ("is_accessible", "is_obstructed_view"),
                    "is_active",
                )
            },
        ),
        (
            "Metadata",
            {"fields": (("created_at", "updated_at"),)},
        ),
    ]

    @admin.display(description="Sector")
    def sector_link(self, obj: models.VenueSeat) -> str:
        url = reverse("admin:events_venuesector_change", args=[obj.sector.id])
        return format_html('<a href="{}">{}</a>', url, str(obj.sector))


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


@admin.register(models.OrganizationQuestionnaire)
class OrganizationQuestionnaireAdmin(ModelAdmin, OrganizationLinkMixin):  # type: ignore[misc]
    list_display = ["__str__", "organization_link", "questionnaire_link"]
    autocomplete_fields = ["organization", "questionnaire"]
    filter_horizontal = ["event_series", "events"]

    def questionnaire_link(self, obj: models.OrganizationQuestionnaire) -> str:
        url = reverse("admin:questionnaires_questionnaire_change", args=[obj.questionnaire.id])
        return format_html('<a href="{}">{}</a>', url, obj.questionnaire.name)

    questionnaire_link.short_description = "Questionnaire"  # type: ignore[attr-defined]


@admin.register(models.AdditionalResource)
class EventResourceAdmin(ModelAdmin):  # type: ignore[misc]
    list_display = ["__str__", "name", "resource_type"]
    list_filter = ["resource_type"]
    search_fields = ["name", "description"]
    autocomplete_fields = ["organization", "events", "event_series"]
    filter_horizontal = ["events"]


# --- Missing Model Admins ---


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
        colors = {
            models.Payment.PaymentStatus.PENDING: "orange",
            models.Payment.PaymentStatus.SUCCEEDED: "green",
            models.Payment.PaymentStatus.FAILED: "red",
            models.Payment.PaymentStatus.REFUNDED: "blue",
        }
        color = colors.get(obj.status, "gray")  # type: ignore[call-overload]
        return mark_safe(f'<span style="color: {color};">{obj.get_status_display()}</span>')

    @admin.display(description="Stripe Session")
    def stripe_session_id_short(self, obj: models.Payment) -> str:
        if obj.stripe_session_id:
            return obj.stripe_session_id[:20] + "..."
        return "-"


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
        colors = {
            models.EventInvitationRequest.InvitationRequestStatus.PENDING: "orange",
            models.EventInvitationRequest.InvitationRequestStatus.APPROVED: "green",
            models.EventInvitationRequest.InvitationRequestStatus.REJECTED: "red",
        }
        color = colors.get(obj.status, "gray")  # type: ignore[call-overload]
        return mark_safe(f'<span style="color: {color};">{obj.get_status_display()}</span>')

    @admin.display(description="Decided By")
    def decided_by_link(self, obj: models.EventInvitationRequest) -> str | None:
        if not obj.decided_by:
            return "—"
        url = reverse("admin:accounts_reveluser_change", args=[obj.decided_by.id])
        return format_html('<a href="{}">{}</a>', url, obj.decided_by.username)


@admin.register(models.MembershipTier)
class MembershipTierAdmin(ModelAdmin, OrganizationLinkMixin):  # type: ignore[misc]
    """Admin for MembershipTier model."""

    list_display = ["__str__", "name", "organization_link", "member_count", "created_at"]
    list_filter = ["organization__name", "created_at"]
    search_fields = ["name", "organization__name"]
    autocomplete_fields = ["organization"]
    readonly_fields = ["created_at", "updated_at"]
    date_hierarchy = "created_at"

    @admin.display(description="Members")
    def member_count(self, obj: models.MembershipTier) -> int:
        return obj.members.count()


@admin.register(models.OrganizationStaff)
class OrganizationStaffAdmin(ModelAdmin, UserLinkMixin, OrganizationLinkMixin):  # type: ignore[misc]
    """Admin for OrganizationStaff model."""

    list_display = ["__str__", "user_link", "organization_link", "permissions_summary", "created_at"]
    list_filter = ["organization__name", "created_at"]
    search_fields = ["user__username", "user__email", "organization__name"]
    autocomplete_fields = ["user", "organization"]
    readonly_fields = ["created_at", "updated_at"]
    date_hierarchy = "created_at"

    @admin.display(description="Permissions")
    def permissions_summary(self, obj: models.OrganizationStaff) -> str:
        if not obj.permissions:
            return "None"
        default_perms = obj.permissions.get("default", {})
        active_perms = [k for k, v in default_perms.items() if v]
        return ", ".join(active_perms) if active_perms else "None"


@admin.register(models.OrganizationMember)
class OrganizationMemberAdmin(ModelAdmin, UserLinkMixin, OrganizationLinkMixin):  # type: ignore[misc]
    """Admin for OrganizationMember model."""

    list_display = ["__str__", "user_link", "organization_link", "status", "tier_name", "created_at"]
    list_filter = ["status", "organization__name", "tier__name", "created_at"]
    search_fields = ["user__username", "user__email", "organization__name"]
    autocomplete_fields = ["user", "organization", "tier"]
    readonly_fields = ["created_at", "updated_at"]
    date_hierarchy = "created_at"

    @admin.display(description="Tier")
    def tier_name(self, obj: models.OrganizationMember) -> str:
        return obj.tier.name if obj.tier else "—"


@admin.register(models.OrganizationMembershipRequest)
class OrganizationMembershipRequestAdmin(ModelAdmin, UserLinkMixin, OrganizationLinkMixin):  # type: ignore[misc]
    """Admin for OrganizationMembershipRequest model."""

    list_display = ["__str__", "user_link", "organization_link", "status_display", "decided_by_link", "created_at"]
    list_filter = ["status", "organization__name", "created_at"]
    search_fields = ["user__username", "user__email", "organization__name", "message"]
    autocomplete_fields = ["user", "organization", "decided_by"]
    readonly_fields = ["created_at", "updated_at"]
    date_hierarchy = "created_at"

    @admin.display(description="Status")
    def status_display(self, obj: models.OrganizationMembershipRequest) -> str:
        colors = {
            models.OrganizationMembershipRequest.Status.PENDING: "orange",
            models.OrganizationMembershipRequest.Status.APPROVED: "green",
            models.OrganizationMembershipRequest.Status.REJECTED: "red",
        }
        color = colors.get(obj.status, "gray")  # type: ignore[call-overload]
        return mark_safe(f'<span style="color: {color};">{obj.get_status_display()}</span>')

    @admin.display(description="Decided By")
    def decided_by_link(self, obj: models.OrganizationMembershipRequest) -> str | None:
        if not obj.decided_by:
            return "—"
        url = reverse("admin:accounts_reveluser_change", args=[obj.decided_by.id])
        return format_html('<a href="{}">{}</a>', url, obj.decided_by.username)


@admin.register(models.OrganizationToken)
class OrganizationTokenAdmin(ModelAdmin, UserLinkMixin, OrganizationLinkMixin):  # type: ignore[misc]
    """Admin for OrganizationToken model."""

    list_display = [
        "name",
        "organization_link",
        "issuer_link",
        "grants_membership",
        "grants_staff_status",
        "tier_name",
        "uses_display",
        "expires_at",
        "created_at",
    ]
    list_filter = ["organization__name", "grants_membership", "grants_staff_status", "expires_at", "created_at"]
    search_fields = ["name", "organization__name", "issuer__username"]
    autocomplete_fields = ["organization", "issuer", "membership_tier"]
    readonly_fields = ["id", "created_at", "updated_at"]
    date_hierarchy = "created_at"

    @admin.display(description="Tier")
    def tier_name(self, obj: models.OrganizationToken) -> str:
        return obj.membership_tier.name if obj.membership_tier else "—"

    @admin.display(description="Issuer")
    def issuer_link(self, obj: models.OrganizationToken) -> str:
        url = reverse("admin:accounts_reveluser_change", args=[obj.issuer.id])
        return format_html('<a href="{}">{}</a>', url, obj.issuer.username)

    @admin.display(description="Uses")
    def uses_display(self, obj: models.OrganizationToken) -> str:
        if obj.max_uses > 0:
            return f"{obj.uses} / {obj.max_uses}"
        return f"{obj.uses} / ∞"


# --- User Preferences Admins ---


@admin.register(models.GeneralUserPreferences)
class GeneralUserPreferencesAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for GeneralUserPreferences."""

    list_display = [
        "__str__",
        "user_link",
        "city_link",
        "show_me_on_attendee_list",
    ]
    list_filter = ["show_me_on_attendee_list", "city__country"]
    search_fields = ["user__username", "user__email", "city__name"]
    autocomplete_fields = ["user", "city"]

    @admin.display(description="User")
    def user_link(self, obj: models.GeneralUserPreferences) -> str:
        url = reverse("admin:accounts_reveluser_change", args=[obj.user.id])
        return format_html('<a href="{}">{}</a>', url, obj.user.username)

    @admin.display(description="City")
    def city_link(self, obj: models.GeneralUserPreferences) -> str | None:
        if not obj.city:
            return "—"
        url = reverse("admin:geo_city_change", args=[obj.city.id])
        return format_html('<a href="{}">{}</a>', url, str(obj.city))


@admin.register(models.AttendeeVisibilityFlag)
class AttendeeVisibilityFlagAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for AttendeeVisibilityFlag model."""

    list_display = ["__str__", "user_link", "target_link", "event_link", "is_visible"]
    list_filter = ["is_visible", "event__organization__name"]
    search_fields = ["user__username", "target__username", "event__name"]
    autocomplete_fields = ["user", "target", "event"]

    @admin.display(description="Viewer")
    def user_link(self, obj: models.AttendeeVisibilityFlag) -> str:
        url = reverse("admin:accounts_reveluser_change", args=[obj.user.id])
        return format_html('<a href="{}">{}</a>', url, obj.user.username)

    @admin.display(description="Target User")
    def target_link(self, obj: models.AttendeeVisibilityFlag) -> str:
        url = reverse("admin:accounts_reveluser_change", args=[obj.target.id])
        return format_html('<a href="{}">{}</a>', url, obj.target.username)

    @admin.display(description="Event")
    def event_link(self, obj: models.AttendeeVisibilityFlag) -> str:
        url = reverse("admin:events_event_change", args=[obj.event.id])
        return format_html('<a href="{}">{}</a>', url, obj.event.name)


# --- Blacklist/Whitelist Admins ---


@admin.register(models.Blacklist)
class BlacklistAdmin(ModelAdmin, OrganizationLinkMixin):  # type: ignore[misc]
    """Admin for Blacklist model."""

    list_display = [
        "__str__",
        "organization_link",
        "user_link",
        "email",
        "telegram_username",
        "name_display",
        "created_by_link",
        "created_at",
    ]
    list_filter = ["organization__name", "created_at"]
    search_fields = [
        "email",
        "telegram_username",
        "phone_number",
        "first_name",
        "last_name",
        "preferred_name",
        "user__username",
        "user__email",
    ]
    autocomplete_fields = ["organization", "user", "created_by"]
    readonly_fields = ["created_at", "updated_at"]
    date_hierarchy = "created_at"
    fieldsets = [
        (None, {"fields": ["organization", "user", "reason", "created_by"]}),
        ("Hard Identifiers", {"fields": ["email", "telegram_username", "phone_number"]}),
        ("Name Fields (for fuzzy matching)", {"fields": ["first_name", "last_name", "preferred_name"]}),
        ("Metadata", {"fields": [("created_at", "updated_at")]}),
    ]

    @admin.display(description="User")
    def user_link(self, obj: models.Blacklist) -> str | None:
        if not obj.user:
            return "—"
        url = reverse("admin:accounts_reveluser_change", args=[obj.user.id])
        return format_html('<a href="{}">{}</a>', url, obj.user.username)

    @admin.display(description="Created By")
    def created_by_link(self, obj: models.Blacklist) -> str | None:
        if not obj.created_by:
            return "—"
        url = reverse("admin:accounts_reveluser_change", args=[obj.created_by.id])
        return format_html('<a href="{}">{}</a>', url, obj.created_by.username)

    @admin.display(description="Name")
    def name_display(self, obj: models.Blacklist) -> str:
        parts = filter(None, [obj.first_name, obj.last_name])
        name = " ".join(parts)
        if obj.preferred_name:
            name = f"{name} ({obj.preferred_name})" if name else obj.preferred_name
        return name or "—"


@admin.register(models.WhitelistRequest)
class WhitelistRequestAdmin(ModelAdmin, UserLinkMixin, OrganizationLinkMixin):  # type: ignore[misc]
    """Admin for WhitelistRequest model."""

    list_display = [
        "__str__",
        "user_link",
        "organization_link",
        "status_display",
        "matched_count",
        "decided_by_link",
        "created_at",
    ]
    list_filter = ["status", "organization__name", "created_at"]
    search_fields = ["user__username", "user__email", "organization__name", "message"]
    autocomplete_fields = ["organization", "user", "decided_by"]
    readonly_fields = ["created_at", "updated_at", "decided_at"]
    filter_horizontal = ["matched_blacklist_entries"]
    date_hierarchy = "created_at"
    fieldsets = [
        (None, {"fields": ["organization", "user", "message"]}),
        ("Status", {"fields": ["status", "decided_by", "decided_at"]}),
        ("Matched Entries", {"fields": ["matched_blacklist_entries"]}),
        ("Metadata", {"fields": [("created_at", "updated_at")]}),
    ]

    @admin.display(description="Status")
    def status_display(self, obj: models.WhitelistRequest) -> str:
        colors = {
            models.WhitelistRequest.Status.PENDING: "orange",
            models.WhitelistRequest.Status.APPROVED: "green",
            models.WhitelistRequest.Status.REJECTED: "red",
        }
        color = colors.get(obj.status, "gray")  # type: ignore[call-overload]
        return mark_safe(f'<span style="color: {color};">{obj.get_status_display()}</span>')

    @admin.display(description="Matched")
    def matched_count(self, obj: models.WhitelistRequest) -> int:
        return obj.matched_blacklist_entries.count()

    @admin.display(description="Decided By")
    def decided_by_link(self, obj: models.WhitelistRequest) -> str | None:
        if not obj.decided_by:
            return "—"
        url = reverse("admin:accounts_reveluser_change", args=[obj.decided_by.id])
        return format_html('<a href="{}">{}</a>', url, obj.decided_by.username)
