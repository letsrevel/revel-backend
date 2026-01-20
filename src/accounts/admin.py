"""Admin interface for accounts app.

This module provides comprehensive admin interfaces for all accounts models
including user management, authentication, and security features.
"""

import typing as t

from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from django.db.models import Count, QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext
from django_google_sso.admin import GoogleSSOInlineAdmin, get_current_user_and_admin
from unfold.admin import ModelAdmin, TabularInline

from accounts.models import (
    DietaryPreference,
    DietaryRestriction,
    EmailVerificationReminderTracking,
    FoodItem,
    ImpersonationLog,
    RevelUser,
    UserDataExport,
    UserDietaryPreference,
)
from accounts.service.account import request_password_reset, send_verification_email_for_user
from accounts.service.impersonation import can_impersonate, create_impersonation_request
from common.models import SiteSettings
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
    fields = ["city"]
    autocomplete_fields = ["city"]


class UserDataExportInline(TabularInline):  # type: ignore[misc]
    """Inline for user data export."""

    model = UserDataExport
    extra = 0
    can_delete = False
    readonly_fields = ["status", "completed_at", "error_message", "file"]
    fields = ["status", "completed_at", "file", "error_message"]


class DietaryRestrictionInline(TabularInline):  # type: ignore[misc]
    """Inline for user dietary restrictions."""

    model = DietaryRestriction
    extra = 0
    autocomplete_fields = ["food_item"]
    fields = ["food_item", "restriction_type", "notes", "is_public"]


class UserDietaryPreferenceInline(TabularInline):  # type: ignore[misc]
    """Inline for user dietary preferences."""

    model = UserDietaryPreference
    extra = 0
    autocomplete_fields = ["preference"]
    fields = ["preference", "comment", "is_public"]


class EmailVerificationReminderTrackingInline(TabularInline):  # type: ignore[misc]
    """Inline for email verification reminder tracking."""

    model = EmailVerificationReminderTracking
    extra = 0
    can_delete = False
    readonly_fields = ["last_reminder_sent_at", "final_warning_sent_at", "deactivation_email_sent_at"]
    fields = ["last_reminder_sent_at", "final_warning_sent_at", "deactivation_email_sent_at"]


@admin.register(RevelUser)
class RevelUserAdmin(UserAdmin, ModelAdmin):  # type: ignore[type-arg,misc]
    """Enhanced admin for RevelUser with comprehensive user management."""

    # List view configuration
    list_display = [
        "profile_thumbnail_display",
        "username",
        "email",
        "display_name_display",
        "pronouns",
        "email_verified_display",
        "totp_active_display",
        "is_staff",
        "is_active",
        "guest",
        "date_joined",
        "event_count",
        "organization_count",
        "impersonate_link",
    ]
    list_filter = [
        "is_staff",
        "is_superuser",
        "is_active",
        "email_verified",
        "totp_active",
        "guest",
        "language",
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
        "profile_picture_thumbnail",
        "profile_picture_preview",
        "profile_image_preview_display",
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
                    "language",
                    "bio",
                )
            },
        ),
        (
            "Profile Picture",
            {
                "fields": (
                    "profile_picture",
                    "profile_image_preview_display",
                ),
            },
        ),
        (
            "Authentication & Security",
            {
                "fields": (
                    "password",
                    ("email_verified", "totp_active"),
                    ("date_joined", "last_login"),
                    "guest",
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
        DietaryRestrictionInline,
        UserDietaryPreferenceInline,
        UserDataExportInline,
        EmailVerificationReminderTrackingInline,
    ]

    # Autocomplete
    autocomplete_fields = ["groups"]

    # Actions
    actions = ["send_verification_email", "send_password_reset_email"]

    @admin.action(description="Send verification email")
    def send_verification_email(self, request: HttpRequest, queryset: QuerySet[RevelUser]) -> None:
        """Send verification email to selected users."""
        sent_count = 0
        skipped_count = 0
        for user in queryset:
            if user.email_verified:
                skipped_count += 1
                continue
            send_verification_email_for_user(user)
            sent_count += 1

        if sent_count:
            self.message_user(
                request,
                ngettext(
                    "Verification email sent to %d user.",
                    "Verification emails sent to %d users.",
                    sent_count,
                )
                % sent_count,
                messages.SUCCESS,
            )
        if skipped_count:
            self.message_user(
                request,
                ngettext(
                    "Skipped %d user (already verified).",
                    "Skipped %d users (already verified).",
                    skipped_count,
                )
                % skipped_count,
                messages.WARNING,
            )

    @admin.action(description="Send password reset email")
    def send_password_reset_email(self, request: HttpRequest, queryset: QuerySet[RevelUser]) -> None:
        """Send password reset email to selected users."""
        sent_count = 0
        skipped_count = 0
        for user in queryset:
            result = request_password_reset(user.email)
            if result:
                sent_count += 1
            else:
                skipped_count += 1

        if sent_count:
            self.message_user(
                request,
                ngettext(
                    "Password reset email sent to %d user.",
                    "Password reset emails sent to %d users.",
                    sent_count,
                )
                % sent_count,
                messages.SUCCESS,
            )
        if skipped_count:
            self.message_user(
                request,
                ngettext(
                    "Skipped %d user (Google SSO or not found).",
                    "Skipped %d users (Google SSO or not found).",
                    skipped_count,
                )
                % skipped_count,
                messages.WARNING,
            )

    # Custom displays
    @admin.display(description="")
    def profile_thumbnail_display(self, obj: RevelUser) -> str:
        """Display profile picture thumbnail in list view."""
        if obj.profile_picture_thumbnail:
            return format_html(
                '<img src="{}" style="width: 32px; height: 32px; border-radius: 50%; object-fit: cover;" />',
                obj.profile_picture_thumbnail.url,
            )
        return format_html(
            '<span style="display: inline-block; width: 32px; height: 32px; '
            "border-radius: 50%; background-color: #e5e7eb; text-align: center; "
            'line-height: 32px; font-size: 14px; color: #6b7280;">{}</span>',
            obj.username[0].upper() if obj.username else "?",
        )

    @admin.display(description="Profile Picture Preview")
    def profile_image_preview_display(self, obj: RevelUser) -> str:
        """Display profile picture preview in detail view."""
        if obj.profile_picture_preview:
            return format_html(
                '<img src="{}" style="max-width: 200px; max-height: 200px; border-radius: 8px;" />',
                obj.profile_picture_preview.url,
            )
        if obj.profile_picture:
            return format_html(
                '<img src="{}" style="max-width: 200px; max-height: 200px; border-radius: 8px;" />',
                obj.profile_picture.url,
            )
        return "No profile picture"

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

    # Impersonation functionality
    @admin.display(description=_("Impersonate"))
    def impersonate_link(self, obj: RevelUser) -> str:
        """Display impersonation link for eligible users."""
        # Don't show link for superusers or staff (can't impersonate them)
        if obj.is_superuser or obj.is_staff:
            return format_html('<span class="text-base-400">—</span>')
        url = reverse("admin:accounts_reveluser_impersonate", args=[obj.pk])
        return format_html(
            '<a href="{}" target="_blank" class="text-primary-600 hover:text-primary-700 '
            'dark:text-primary-500 dark:hover:text-primary-400 text-sm font-medium">{}</a>',
            url,
            _("Impersonate"),
        )

    def get_urls(self) -> list[t.Any]:
        """Add custom URLs for impersonation."""
        urls = super().get_urls()
        custom_urls = [
            path(
                "<uuid:user_id>/impersonate/",
                self.admin_site.admin_view(self.impersonate_view),
                name="accounts_reveluser_impersonate",
            ),
        ]
        return custom_urls + urls

    def impersonate_view(
        self, request: HttpRequest, user_id: t.Any
    ) -> HttpResponse | HttpResponseRedirect | TemplateResponse:
        """Handle impersonation confirmation and token generation."""
        target_user = get_object_or_404(RevelUser, pk=user_id)
        admin_user = t.cast(RevelUser, request.user)

        # Check permissions
        allowed, error = can_impersonate(admin_user, target_user)

        if request.method == "POST" and allowed:
            # Get client info for audit
            ip_address = self._get_client_ip(request)
            user_agent = request.META.get("HTTP_USER_AGENT", "")

            # Generate impersonation token
            token, _log = create_impersonation_request(
                admin=admin_user,
                target=target_user,
                ip_address=ip_address,
                user_agent=user_agent,
            )

            # Redirect to frontend with token
            site_settings = SiteSettings.get_solo()
            frontend_url = f"{site_settings.frontend_base_url}/impersonate?token={token}"
            return HttpResponseRedirect(frontend_url)

        # GET: Show confirmation page
        context = {
            **self.admin_site.each_context(request),
            "title": _("Impersonate User"),
            "target_user": target_user,
            "opts": self.model._meta,
            "error": error if not allowed else None,
        }
        return TemplateResponse(request, "admin/accounts/impersonate_confirm.html", context)

    def _get_client_ip(self, request: HttpRequest) -> str | None:
        """Extract client IP address from request."""
        x_forwarded_for: str | None = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            return x_forwarded_for.split(",")[0].strip()
        remote_addr: str | None = request.META.get("REMOTE_ADDR")
        return remote_addr


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


# --- Dietary Models Admins ---


@admin.register(FoodItem)
class FoodItemAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for FoodItem model."""

    list_display = ["name", "restriction_count", "created_at"]
    search_fields = ["name"]
    readonly_fields = ["id", "created_at", "updated_at"]
    ordering = ["name"]
    date_hierarchy = "created_at"

    def get_queryset(self, request: t.Any) -> t.Any:
        """Override to annotate restriction count."""
        qs = super().get_queryset(request)
        return qs.annotate(restrictions_count=Count("user_restrictions"))

    @admin.display(description="Restrictions", ordering="restrictions_count")
    def restriction_count(self, obj: FoodItem) -> int:
        return obj.restrictions_count  # type: ignore[no-any-return, attr-defined]

    def has_add_permission(self, request: t.Any) -> bool:
        # Food items are created through dietary restrictions
        return t.cast(bool, request.user.is_superuser)

    def has_delete_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        # Prevent deletion to maintain data integrity
        return False


@admin.register(DietaryRestriction)
class DietaryRestrictionAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for DietaryRestriction model."""

    list_display = [
        "__str__",
        "user_link",
        "food_item_link",
        "restriction_type",
        "restriction_type_display",
        "is_public",
        "created_at",
    ]
    list_filter = ["restriction_type", "is_public", "created_at"]
    search_fields = ["user__username", "user__email", "food_item__name", "notes"]
    autocomplete_fields = ["user", "food_item"]
    readonly_fields = ["id", "created_at", "updated_at"]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]

    fieldsets = [
        (
            "Basic Information",
            {
                "fields": (
                    "user",
                    "food_item",
                    "restriction_type",
                )
            },
        ),
        (
            "Details",
            {
                "fields": (
                    "notes",
                    "is_public",
                )
            },
        ),
        (
            "Metadata",
            {
                "fields": (
                    "id",
                    "created_at",
                    "updated_at",
                ),
                "classes": ["collapse"],
            },
        ),
    ]

    @admin.display(description="User")
    def user_link(self, obj: DietaryRestriction) -> str:
        url = reverse("admin:accounts_reveluser_change", args=[obj.user.id])
        return format_html('<a href="{}">{}</a>', url, obj.user.username)

    @admin.display(description="Food Item")
    def food_item_link(self, obj: DietaryRestriction) -> str:
        url = reverse("admin:accounts_fooditem_change", args=[obj.food_item.id])
        return format_html('<a href="{}">{}</a>', url, obj.food_item.name)

    @admin.display(description="Severity")
    def restriction_type_display(self, obj: DietaryRestriction) -> str:
        colors = {
            DietaryRestriction.RestrictionType.DISLIKE: "gray",
            DietaryRestriction.RestrictionType.INTOLERANT: "orange",
            DietaryRestriction.RestrictionType.ALLERGY: "red",
            DietaryRestriction.RestrictionType.SEVERE_ALLERGY: "darkred",
        }
        color = colors.get(obj.restriction_type, "gray")  # type: ignore[call-overload]
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            color,
            obj.get_restriction_type_display(),
        )


@admin.register(DietaryPreference)
class DietaryPreferenceAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for DietaryPreference model (system-managed)."""

    list_display = ["name", "user_count", "created_at"]
    search_fields = ["name"]
    readonly_fields = ["id", "created_at", "updated_at"]
    ordering = ["name"]
    date_hierarchy = "created_at"

    def get_queryset(self, request: t.Any) -> t.Any:
        """Override to annotate user count."""
        qs = super().get_queryset(request)
        return qs.annotate(users_count=Count("users"))

    @admin.display(description="Users", ordering="users_count")
    def user_count(self, obj: DietaryPreference) -> int:
        return obj.users_count  # type: ignore[no-any-return, attr-defined]

    def has_delete_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        # System-managed preferences should not be easily deleted
        return t.cast(bool, request.user.is_superuser)


@admin.register(UserDietaryPreference)
class UserDietaryPreferenceAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for UserDietaryPreference model."""

    list_display = [
        "__str__",
        "user_link",
        "preference_link",
        "comment_preview",
        "is_public",
        "created_at",
    ]
    list_filter = ["preference__name", "is_public", "created_at"]
    search_fields = ["user__username", "user__email", "preference__name", "comment"]
    autocomplete_fields = ["user", "preference"]
    readonly_fields = ["id", "created_at", "updated_at"]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]

    fieldsets = [
        (
            "Basic Information",
            {
                "fields": (
                    "user",
                    "preference",
                )
            },
        ),
        (
            "Details",
            {
                "fields": (
                    "comment",
                    "is_public",
                )
            },
        ),
        (
            "Metadata",
            {
                "fields": (
                    "id",
                    "created_at",
                    "updated_at",
                ),
                "classes": ["collapse"],
            },
        ),
    ]

    @admin.display(description="User")
    def user_link(self, obj: UserDietaryPreference) -> str:
        url = reverse("admin:accounts_reveluser_change", args=[obj.user.id])
        return format_html('<a href="{}">{}</a>', url, obj.user.username)

    @admin.display(description="Preference")
    def preference_link(self, obj: UserDietaryPreference) -> str:
        url = reverse("admin:accounts_dietarypreference_change", args=[obj.preference.id])
        return format_html('<a href="{}">{}</a>', url, obj.preference.name)

    @admin.display(description="Comment")
    def comment_preview(self, obj: UserDietaryPreference) -> str:
        if obj.comment:
            return obj.comment[:50] + ("..." if len(obj.comment) > 50 else "")
        return "—"


# --- Impersonation Audit Log ---


@admin.register(ImpersonationLog)
class ImpersonationLogAdmin(ModelAdmin):  # type: ignore[misc]
    """Read-only admin for impersonation audit logs."""

    list_display = [
        "created_at",
        "admin_user_link",
        "target_user_link",
        "status_display",
        "redeemed_at",
        "ip_address",
    ]
    list_filter = ["created_at", "redeemed_at"]
    search_fields = [
        "admin_user__username",
        "admin_user__email",
        "target_user__username",
        "target_user__email",
        "ip_address",
    ]
    readonly_fields = [
        "id",
        "admin_user",
        "target_user",
        "created_at",
        "ip_address",
        "user_agent",
        "token_jti",
        "redeemed_at",
    ]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]

    fieldsets = [
        (
            _("Impersonation Details"),
            {
                "fields": (
                    "id",
                    ("admin_user", "target_user"),
                    ("created_at", "redeemed_at"),
                )
            },
        ),
        (
            _("Request Information"),
            {
                "fields": (
                    "ip_address",
                    "user_agent",
                    "token_jti",
                ),
                "classes": ["collapse"],
            },
        ),
    ]

    @admin.display(description=_("Admin"))
    def admin_user_link(self, obj: ImpersonationLog) -> str:
        url = reverse("admin:accounts_reveluser_change", args=[obj.admin_user.id])
        return format_html('<a href="{}">{}</a>', url, obj.admin_user.email)

    @admin.display(description=_("Target User"))
    def target_user_link(self, obj: ImpersonationLog) -> str:
        url = reverse("admin:accounts_reveluser_change", args=[obj.target_user.id])
        return format_html('<a href="{}">{}</a>', url, obj.target_user.email)

    @admin.display(description=_("Status"))
    def status_display(self, obj: ImpersonationLog) -> str:
        if obj.is_redeemed:
            return format_html(
                '<span class="text-green-600 dark:text-green-400 font-medium">{}</span>',
                _("Redeemed"),
            )
        return format_html(
            '<span class="text-orange-600 dark:text-orange-400 font-medium">{}</span>',
            _("Pending"),
        )

    def has_add_permission(self, request: t.Any) -> bool:
        """Prevent manual creation of audit logs."""
        return False

    def has_change_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        """Prevent modification of audit logs."""
        return False

    def has_delete_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        """Prevent deletion of audit logs (only superusers can delete for compliance)."""
        return t.cast(bool, request.user.is_superuser)
