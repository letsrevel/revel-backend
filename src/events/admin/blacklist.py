# src/events/admin/blacklist.py
"""Admin classes for Blacklist and WhitelistRequest models."""

import typing as t

from django.contrib import admin
from django.db.models import Count
from django.http import Http404, HttpRequest
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin
from unfold.contrib.filters.admin import AutocompleteSelectFilter

from events import models
from events.admin.base import (
    CreatedByDefaultMixin,
    OrganizationLinkMixin,
    UserLinkMixin,
)


@admin.register(models.Blacklist)
class BlacklistAdmin(CreatedByDefaultMixin, ModelAdmin, OrganizationLinkMixin):  # type: ignore[misc]
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
    list_select_related = ["organization", "user", "created_by"]
    list_filter = [("organization", AutocompleteSelectFilter), "created_at"]
    list_filter_submit = True
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

    def get_readonly_fields(self, request: HttpRequest, obj: models.Blacklist | None = None) -> t.Any:
        """On an existing entry, only the reason is editable.

        The organization, the linked account and the hard identifiers cannot change after creation (as in
        ``update_blacklist_entry``). The names are read-only too: an account deletion clears them, and a change
        form loaded before it must not write them back.
        """
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            fields += [
                "organization",
                "user",
                "created_by",
                "email",
                "telegram_username",
                "phone_number",
                "first_name",
                "last_name",
                "preferred_name",
            ]
        return fields

    def save_model(self, request: HttpRequest, obj: models.Blacklist, form: t.Any, change: bool) -> None:
        """Take the organization lock first, like a manual add and the entries kept after an account deletion.

        So an admin save cannot insert an identifier between their check and their write. A change writes only
        the edited fields: the admin loaded the row before it took the lock, and a full save would write back
        what an account deletion changed in between. If the deletion removed the row, the save answers 404.
        """
        from events.service.blacklist_service import lock_organization_for_staff_changes

        lock_organization_for_staff_changes(obj.organization)
        if change:
            if not models.Blacklist.objects.filter(pk=obj.pk).exists():
                raise Http404("This entry was removed by an account deletion. Reload the list.")
            if changed := (form.changed_data if form is not None else []):
                obj.save(update_fields=[*changed, "updated_at"])
            obj.refresh_from_db()  # the admin's log message reads the current row, not the one loaded before the lock
            return
        super().save_model(request, obj, form, change)

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
    list_select_related = ["user", "organization", "decided_by"]
    list_filter = ["status", ("organization", AutocompleteSelectFilter), "created_at"]
    list_filter_submit = True
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
        colors: dict[t.Any, str] = {
            models.WhitelistRequest.Status.PENDING: "orange",
            models.WhitelistRequest.Status.APPROVED: "green",
            models.WhitelistRequest.Status.REJECTED: "red",
        }
        color = colors.get(obj.status, "gray")
        return mark_safe(f'<span style="color: {color};">{obj.get_status_display()}</span>')

    def get_queryset(self, request: t.Any) -> t.Any:
        return super().get_queryset(request).annotate(_matched_count=Count("matched_blacklist_entries"))

    @admin.display(description="Matched", ordering="_matched_count")
    def matched_count(self, obj: models.WhitelistRequest) -> int:
        return t.cast(int, getattr(obj, "_matched_count", 0))

    @admin.display(description="Decided By")
    def decided_by_link(self, obj: models.WhitelistRequest) -> str | None:
        if not obj.decided_by:
            return "—"
        url = reverse("admin:accounts_reveluser_change", args=[obj.decided_by.id])
        return format_html('<a href="{}">{}</a>', url, obj.decided_by.username)
