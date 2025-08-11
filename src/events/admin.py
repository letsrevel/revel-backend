# src/events/admin.py

import typing as t

from django import forms
from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from unfold.admin import ModelAdmin, StackedInline, TabularInline
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


class OrganizationSettingsForm(forms.ModelForm):  # type: ignore[type-arg]
    """Custom form to provide choices for the JSONField."""

    membership_requests_methods = forms.MultipleChoiceField(
        choices=[(v, v) for v in models.ALLOWED_MEMBERSHIP_REQUEST_METHODS],
        widget=forms.CheckboxSelectMultiple,
        required=False,
    )

    class Meta:
        model = models.OrganizationSettings
        fields = "__all__"


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
    autocomplete_fields = ["user"]


class OrganizationSettingsInline(StackedInline):  # type: ignore[misc]
    model = models.OrganizationSettings
    form = OrganizationSettingsForm


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
class TicketTierAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin view for TicketTier."""

    list_display = ["__str__", "name", "event", "short_description"]
    list_filter = ["event"]
    search_fields = ["name", "event__name", "description"]

    def short_description(self, obj: models.TicketTier) -> str:
        return obj.description[:100] if obj.description else "-"

    short_description.short_description = "Description"  # type: ignore[attr-defined]


@admin.register(models.Organization)
class OrganizationAdmin(ModelAdmin, UserLinkMixin):  # type: ignore[misc]
    """Admin model for Organizations."""

    list_display = ["name", "slug", "owner_link", "visibility", "created_at"]
    search_fields = ["name", "slug", "owner__username"]
    autocomplete_fields = ["owner", "city", "staff_members", "members"]
    prepopulated_fields = {"slug": ("name",)}

    tabs = [
        ("Settings", ["Settings"]),
        ("People", ["Staff", "Members"]),
        ("Content", ["Series", "Questionnaires"]),
    ]

    inlines = [
        OrganizationSettingsInline,
        OrganizationStaffInline,
        OrganizationMemberInline,
        EventSeriesInline,
        OrganizationQuestionnaireInline,
    ]

    def owner_link(self, obj: models.Organization) -> str:
        return self.user_link(obj)

    owner_link.short_description = "Owner"  # type: ignore[attr-defined]


@admin.register(models.Event)
class EventAdmin(ModelAdmin, OrganizationLinkMixin):  # type: ignore[misc]
    """Admin model for Events."""

    list_display = ["name", "organization_link", "event_type", "start", "attendee_count", "requires_ticket"]
    list_filter = ["event_type", "organization", "start", "requires_ticket", "waitlist_open"]
    search_fields = ["name", "slug", "organization__name"]
    autocomplete_fields = ["organization", "event_series", "city"]
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
                    ("event_type", "visibility"),
                    "start",
                    "event_series",
                    ("max_attendees", "waitlist_open"),
                    ("free_for_members", "free_for_staff"),
                    "requires_ticket",
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
            count = obj.rsvps.filter(status=models.EventRSVP.Status.YES).count()

        limit = obj.max_attendees if obj.max_attendees > 0 else "∞"
        return f"{count} / {limit}"


@admin.register(models.EventSeries)
class EventSeriesAdmin(ModelAdmin, OrganizationLinkMixin):  # type: ignore[misc]
    list_display = ["name", "slug", "organization_link"]
    search_fields = ["name", "slug", "organization__name"]
    autocomplete_fields = ["organization"]
    prepopulated_fields = {"slug": ("name",)}


@admin.register(models.Ticket)
class TicketAdmin(ModelAdmin, UserLinkMixin, EventLinkMixin):  # type: ignore[misc]
    list_display = ["id", "event_link", "user_link", "tier_name", "status", "checked_in_at"]
    list_filter = ["status", "event__name", "tier__name"]
    search_fields = ["event__name", "user__username"]
    autocomplete_fields = ["event", "user", "tier", "checked_in_by"]
    readonly_fields = ["id", "checked_in_at", "checked_in_by"]
    date_hierarchy = "created_at"

    @admin.display(description="Tier")
    def tier_name(self, obj: models.Ticket) -> str | None:
        return obj.tier.name if obj.tier else "—"


@admin.register(models.EventInvitation)
class EventInvitationAdmin(ModelAdmin, UserLinkMixin, EventLinkMixin):  # type: ignore[misc]
    list_display = ["user_link", "event_link", "tier_name", "waives_questionnaire", "waives_purchase"]
    list_filter = ["event__name", "tier__name"]
    search_fields = ["user__username", "event__name"]
    autocomplete_fields = ["user", "event", "tier"]

    @admin.display(description="Tier")
    def tier_name(self, obj: models.EventInvitation) -> str | None:
        return obj.tier.name if obj.tier else "—"


@admin.register(models.EventRSVP)
class EventRSVPAdmin(ModelAdmin, UserLinkMixin, EventLinkMixin):  # type: ignore[misc]
    list_display = ["user_link", "event_link", "status"]
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
    filter_horizontal = ["events"]
