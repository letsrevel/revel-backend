"""Admin interface for accounts app.

This module provides comprehensive admin interfaces for all accounts models
including user management, authentication, and security features.
"""

import typing as t

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django_google_sso.admin import GoogleSSOInlineAdmin, get_current_user_and_admin
from unfold.admin import ModelAdmin, TabularInline

from accounts.models import RevelUser, UserDataExport
from events.models import GeneralUserPreferences

# Monkey patch the missing attribute
GoogleSSOInlineAdmin.ordering_field = "google_id"

CurrentUserModel, last_admin, LastUserAdmin = get_current_user_and_admin()


if admin.site.is_registered(CurrentUserModel):
    admin.site.unregister(CurrentUserModel)


class GeneralUserPreferencesInline(TabularInline):  # type: ignore[misc]
    """Inline for user preferences."""

    model = GeneralUserPreferences
    extra = 0
    can_delete = False
    fields = ["silence_all_notifications", "show_me_on_attendee_list", "event_reminders", "city"]
    autocomplete_fields = ["city"]


class UserDataExportInline(TabularInline):  # type: ignore[misc]
    """Inline for user data export."""

    model = UserDataExport
    extra = 0
    can_delete = False
    readonly_fields = ["status", "completed_at", "error_message", "file"]
    fields = ["status", "completed_at", "file", "error_message"]


@admin.register(RevelUser)
class RevelUserAdmin(UserAdmin, ModelAdmin):  # type: ignore[type-arg,misc]
    """Enhanced admin for RevelUser with comprehensive user management."""

    # List view configuration
    list_display = [
        "username",
        "email",
        "display_name_display",
        "email_verified_display",
        "totp_active_display",
        "is_staff",
        "is_active",
        "date_joined",
        "event_count",
        "organization_count",
    ]
    list_filter = [
        "is_staff",
        "is_superuser",
        "is_active",
        "email_verified",
        "totp_active",
        "date_joined",
        "last_login",
    ]
    search_fields = ["username", "first_name", "last_name", "email", "preferred_name", "phone_number"]
    ordering = ["-date_joined"]
    date_hierarchy = "date_joined"

    # Detail view configuration
    readonly_fields = [
        "id",
        "date_joined",
        "last_login",
        "display_name_display",
        "event_participation_display",
        "organization_participation_display",
    ]

    fieldsets = (
        (
            "Personal Information",
            {
                "fields": (
                    "id",
                    ("username", "email"),
                    ("first_name", "last_name"),
                    ("preferred_name", "pronouns"),
                    "phone_number",
                    "display_name_display",
                )
            },
        ),
        (
            "Authentication & Security",
            {
                "fields": (
                    "password",
                    ("email_verified", "totp_active"),
                    ("date_joined", "last_login"),
                )
            },
        ),
        (
            "Permissions",
            {
                "fields": (
                    ("is_active", "is_staff", "is_superuser"),
                    "groups",
                    "user_permissions",
                ),
                "classes": ["collapse"],
            },
        ),
        (
            "Participation Summary",
            {
                "fields": (
                    "event_participation_display",
                    "organization_participation_display",
                ),
                "classes": ["collapse"],
            },
        ),
    )

    # Inlines
    inlines = [
        GeneralUserPreferencesInline,
        UserDataExportInline,
    ]

    # Autocomplete
    autocomplete_fields = ["groups"]

    # Custom displays
    @admin.display(description="Display Name", ordering="preferred_name")
    def display_name_display(self, obj: RevelUser) -> str:
        return obj.get_display_name()

    @admin.display(description="Email Verified", boolean=True)
    def email_verified_display(self, obj: RevelUser) -> bool:
        return obj.email_verified

    @admin.display(description="2FA Active", boolean=True)
    def totp_active_display(self, obj: RevelUser) -> bool:
        return obj.totp_active

    @admin.display(description="Events")
    def event_count(self, obj: RevelUser) -> int:
        # Count events user has tickets for or RSVP'd to
        from events.models import EventRSVP, Ticket

        ticket_events = Ticket.objects.filter(user=obj).values("event").distinct().count()
        rsvp_events = EventRSVP.objects.filter(user=obj).values("event").distinct().count()
        return ticket_events + rsvp_events

    @admin.display(description="Organizations")
    def organization_count(self, obj: RevelUser) -> int:
        # Count owned + member + staff organizations
        from events.models import Organization, OrganizationMember, OrganizationStaff

        owned = Organization.objects.filter(owner=obj).count()
        member = OrganizationMember.objects.filter(user=obj).count()
        staff = OrganizationStaff.objects.filter(user=obj).count()
        return owned + member + staff

    def event_participation_display(self, obj: RevelUser) -> str:
        """Display user's event participation details."""
        from events.models import EventRSVP, Ticket

        tickets = Ticket.objects.filter(user=obj).select_related("event")
        rsvps = EventRSVP.objects.filter(user=obj).select_related("event")

        html = "<h4>Event Participation:</h4><ul>"

        for ticket in tickets[:10]:  # Show recent 10
            event_url = reverse("admin:events_event_change", args=[ticket.event.id])
            html += f'<li><a href="{event_url}">{ticket.event.name}</a> - Ticket ({ticket.status})</li>'

        for rsvp in rsvps[:10]:  # Show recent 10
            event_url = reverse("admin:events_event_change", args=[rsvp.event.id])
            html += f'<li><a href="{event_url}">{rsvp.event.name}</a> - RSVP ({rsvp.status})</li>'

        html += "</ul>"
        return mark_safe(html)

    event_participation_display.short_description = "Event Participation"  # type: ignore[attr-defined]

    def organization_participation_display(self, obj: RevelUser) -> str:
        """Display user's organization participation details."""
        from events.models import Organization, OrganizationMember, OrganizationStaff

        owned_orgs = Organization.objects.filter(owner=obj)
        staff_orgs = OrganizationStaff.objects.filter(user=obj).select_related("organization")
        member_orgs = OrganizationMember.objects.filter(user=obj).select_related("organization")

        html = "<h4>Organization Roles:</h4><ul>"

        for org in owned_orgs:
            org_url = reverse("admin:events_organization_change", args=[org.id])
            html += f'<li><a href="{org_url}">{org.name}</a> - Owner</li>'

        for staff in staff_orgs:
            org_url = reverse("admin:events_organization_change", args=[staff.organization.id])
            html += f'<li><a href="{org_url}">{staff.organization.name}</a> - Staff</li>'

        for member in member_orgs:
            org_url = reverse("admin:events_organization_change", args=[member.organization.id])
            html += f'<li><a href="{org_url}">{member.organization.name}</a> - Member</li>'

        html += "</ul>"
        return mark_safe(html)

    organization_participation_display.short_description = "Organization Participation"  # type: ignore[attr-defined]


@admin.register(UserDataExport)
class UserDataExportAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for user data exports with GDPR compliance features."""

    list_display = ["user_link", "status", "status_display", "completed_at", "file_size", "created_at"]
    list_filter = ["status", "created_at", "completed_at"]
    search_fields = ["user__username", "user__email"]
    readonly_fields = [
        "id",
        "user",
        "file",
        "status",
        "error_message",
        "completed_at",
        "created_at",
        "updated_at",
        "file_size",
    ]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]

    def user_link(self, obj: UserDataExport) -> str:
        url = reverse("admin:accounts_reveluser_change", args=[obj.user.id])
        return format_html('<a href="{}">{}</a>', url, obj.user.username)

    user_link.short_description = "User"  # type: ignore[attr-defined]
    user_link.admin_order_field = "user__username"  # type: ignore[attr-defined]

    @admin.display(description="Status")
    def status_display(self, obj: UserDataExport) -> str:
        colors = {
            UserDataExport.Status.PENDING: "orange",
            UserDataExport.Status.PROCESSING: "blue",
            UserDataExport.Status.READY: "green",
            UserDataExport.Status.FAILED: "red",
        }
        color = colors.get(obj.status, "gray")  # type: ignore[call-overload]
        return mark_safe(f'<span style="color: {color};">{obj.get_status_display()}</span>')

    def file_size(self, obj: UserDataExport) -> str:
        if obj.file:
            size = obj.file.size
            if size < 1024:
                return f"{size} B"
            elif size < 1024 * 1024:
                return f"{size // 1024} KB"
            else:
                return f"{size // (1024 * 1024)} MB"
        return "-"

    file_size.short_description = "File Size"  # type: ignore[attr-defined]

    def has_add_permission(self, request: t.Any) -> bool:
        return False
