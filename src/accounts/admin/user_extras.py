"""Filters and read-only inlines for ``RevelUserAdmin``.

Split out of ``accounts/admin/user.py`` to respect the 1000-line file limit.
"""

import typing as t

from django.contrib import admin
from django.db.models import Exists, OuterRef, QuerySet
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _
from unfold.admin import TabularInline

from accounts.models import GlobalBan, ReferralCode, RevelUser


class ReferralCodeInline(TabularInline):  # type: ignore[misc]
    """Read-only inline showing the user's referral code."""

    model = ReferralCode
    extra = 0
    can_delete = False
    readonly_fields = ["code", "is_active", "created_at"]
    fields = readonly_fields

    def has_add_permission(self, request: HttpRequest, obj: t.Any = None) -> bool:
        return False


class GlobalBanInline(TabularInline):  # type: ignore[misc]
    """Read-only inline showing global bans linked to the user."""

    model = GlobalBan
    fk_name = "user"
    extra = 0
    can_delete = False
    readonly_fields = ["ban_type", "value", "reason", "created_by", "created_at"]
    fields = readonly_fields
    verbose_name_plural = "Global bans"

    def has_add_permission(self, request: HttpRequest, obj: t.Any = None) -> bool:
        return False


class IsOrganizationOwnerFilter(admin.SimpleListFilter):
    """Filter users by whether they own at least one organization."""

    title = _("organization owner")
    parameter_name = "is_owner"

    def lookups(self, request: HttpRequest, model_admin: admin.ModelAdmin) -> list[tuple[str, str]]:  # type: ignore[type-arg]
        return [("yes", str(_("Yes"))), ("no", str(_("No")))]

    def queryset(self, request: HttpRequest, queryset: QuerySet[RevelUser]) -> QuerySet[RevelUser]:
        from events.models import Organization

        owns_org = Exists(Organization.objects.filter(owner=OuterRef("pk")))
        if self.value() == "yes":
            return queryset.filter(owns_org)
        if self.value() == "no":
            return queryset.filter(~owns_org)
        return queryset
