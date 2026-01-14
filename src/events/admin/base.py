# src/events/admin/base.py
"""Base admin components: mixins, forms, and inlines."""

import typing as t

from django import forms
from django.urls import reverse
from django.utils.html import format_html
from unfold.admin import TabularInline
from unfold.widgets import CHECKBOX_CLASSES

from events import models


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
