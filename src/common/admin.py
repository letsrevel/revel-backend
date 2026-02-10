import typing as t

from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from unfold.admin import ModelAdmin

from . import models


class UploaderLinkMixin:
    """Mixin to add a link to an uploader."""

    def uploader_link(self, obj: t.Any) -> str:
        from accounts.models import RevelUser

        try:
            user = RevelUser.objects.get(email=obj.uploader)
            url = reverse("admin:accounts_reveluser_change", args=[user.id])
            return format_html('<a href="{}">{}</a>', url, obj.uploader)
        except RevelUser.DoesNotExist:
            return obj.uploader  # type: ignore[no-any-return]

    uploader_link.short_description = "Uploader"  # type: ignore[attr-defined]


@admin.register(models.Legal)
class LegalAdmin(ModelAdmin):  # type: ignore[misc]
    list_display = ["__str__", "updated_at"]
    readonly_fields = ["updated_at"]
    search_fields = ["terms_and_conditions", "privacy_policy"]


@admin.register(models.SiteSettings)
class SiteSettingsAdmin(ModelAdmin):  # type: ignore[misc]
    list_display = [
        "__str__",
        "notify_user_joined",
        "live_emails",
        "data_retention_days",
        "updated_at",
    ]
    readonly_fields = ["created_at", "updated_at"]
    fieldsets = (
        ("Notifications", {"fields": ("notify_user_joined", "live_emails")}),
        ("Data Management", {"fields": ("data_retention_days",)}),
        ("URLs & Emails", {"fields": ("frontend_base_url", "internal_catchall_email")}),
        (
            "Maintenance Banner",
            {
                "fields": (
                    "maintenance_message",
                    "maintenance_severity",
                    "maintenance_scheduled_at",
                    "maintenance_ends_at",
                ),
                "description": "Configure a maintenance banner shown to all users. Leave the message blank to disable.",
            },
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )


@admin.register(models.EmailLog)
class EmailLogAdmin(ModelAdmin):  # type: ignore[misc]
    list_display = ["to", "subject", "sent_at", "test_only"]
    list_filter = ["test_only", "sent_at"]
    search_fields = ["to", "subject"]
    readonly_fields = ["id", "created_at", "updated_at", "sent_at", "body", "html"]
    date_hierarchy = "sent_at"
    ordering = ["-sent_at"]

    def has_add_permission(self, request: t.Any) -> bool:
        return False

    def has_change_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        return False


@admin.register(models.Tag)
class TagAdmin(ModelAdmin):  # type: ignore[misc]
    list_display = ["name", "description", "color", "parent", "created_at"]
    list_filter = ["parent", "created_at"]
    search_fields = ["name", "description"]
    readonly_fields = ["id", "created_at", "updated_at"]
    ordering = ["name"]
    autocomplete_fields = ["parent"]


@admin.register(models.TagAssignment)
class TagAssignmentAdmin(ModelAdmin):  # type: ignore[misc]
    list_display = ["tag", "content_type", "object_id", "assigned_by", "created_at"]
    list_filter = ["content_type", "created_at"]
    search_fields = ["tag__name"]
    readonly_fields = ["id", "created_at", "updated_at"]
    autocomplete_fields = ["tag"]
    ordering = ["-created_at"]


@admin.register(models.FileUploadAudit)
class FileUploadAuditAdmin(ModelAdmin, UploaderLinkMixin):  # type: ignore[misc]
    list_display = [
        "uploader_link",
        "app",
        "model",
        "field",
        "status",
        "file_hash",
        "created_at",
    ]
    list_filter = ["status", "app", "model", "created_at"]
    search_fields = ["uploader", "file_hash", "instance_pk"]
    readonly_fields = ["id", "created_at", "updated_at"]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]

    def has_add_permission(self, request: t.Any) -> bool:
        return False

    def has_change_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        return False


@admin.register(models.QuarantinedFile)
class QuarantinedFileAdmin(ModelAdmin, UploaderLinkMixin):  # type: ignore[misc]
    list_display = [
        "audit_link",
        "uploader_link",
        "file",
        "findings_summary",
        "created_at",
    ]
    list_filter = ["created_at"]
    search_fields = ["audit__uploader", "audit__file_hash"]
    readonly_fields = ["id", "created_at", "updated_at", "findings"]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]

    def get_queryset(self, request: t.Any) -> t.Any:
        return super().get_queryset(request).select_related("audit")

    def audit_link(self, obj: models.QuarantinedFile) -> str:
        url = reverse("admin:common_fileuploadaudit_change", args=[obj.audit.id])
        return format_html(
            '<a href="{}">{} - {} - {}</a>',
            url,
            obj.audit.app,
            obj.audit.model,
            obj.audit.field,
        )

    audit_link.short_description = "Audit"  # type: ignore[attr-defined]

    def uploader_link(self, obj: models.QuarantinedFile) -> str:
        from accounts.models import RevelUser

        try:
            user = RevelUser.objects.get(email=obj.audit.uploader)
            url = reverse("admin:accounts_reveluser_change", args=[user.id])
            return format_html('<a href="{}">{}</a>', url, obj.audit.uploader)
        except RevelUser.DoesNotExist:
            return obj.audit.uploader

    uploader_link.short_description = "Uploader"  # type: ignore[attr-defined]

    def findings_summary(self, obj: t.Any) -> str:
        findings = obj.findings or {}
        if not findings:
            return "No findings"
        # Extract malware names from findings
        malware_names = []
        for key, value in findings.items():
            if isinstance(value, tuple) and len(value) >= 2:
                malware_names.append(value[1])
            elif isinstance(value, str) and value != "OK":
                malware_names.append(value)
        return ", ".join(malware_names) if malware_names else "Unknown threat"

    findings_summary.short_description = "Threats Found"  # type: ignore[attr-defined]

    def has_add_permission(self, request: t.Any) -> bool:
        return False

    def has_change_permission(self, request: t.Any, obj: t.Any = None) -> bool:
        return False
