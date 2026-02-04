# src/events/admin/organization.py
"""Admin classes for Organization and related models."""

import typing as t

from django.contrib import admin
from django.db.models import Count, OuterRef, QuerySet, Subquery
from django.http import HttpRequest
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin

from events import models
from events.admin.base import (
    EventSeriesInline,
    MembershipTierInline,
    OrganizationLinkMixin,
    OrganizationMemberInline,
    OrganizationQuestionnaireInline,
    OrganizationStaffInline,
    UserLinkMixin,
    VenueInline,
)


@admin.register(models.Organization)
class OrganizationAdmin(ModelAdmin, UserLinkMixin):  # type: ignore[misc]
    """Admin model for Organizations."""

    list_display = [
        "name",
        "slug",
        "owner_link",
        "members_count",
        "events_count",
        "stripe_connected",
        "visibility",
        "created_at",
    ]
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

    def get_queryset(self, request: HttpRequest) -> QuerySet[models.Organization]:
        qs: QuerySet[models.Organization] = super().get_queryset(request)
        # Use subqueries to avoid Cartesian product from joining both tables
        members_subquery = (
            models.OrganizationMember.objects.filter(organization=OuterRef("pk"))
            .values("organization")
            .annotate(cnt=Count("id"))
            .values("cnt")
        )
        events_subquery = (
            models.Event.objects.filter(organization=OuterRef("pk"))
            .values("organization")
            .annotate(cnt=Count("id"))
            .values("cnt")
        )
        return qs.annotate(
            _members_count=Subquery(members_subquery),
            _events_count=Subquery(events_subquery),
        )

    def owner_link(self, obj: models.Organization) -> str:
        return self.user_link(obj)

    owner_link.short_description = "Owner"  # type: ignore[attr-defined]

    @admin.display(description="Members", ordering="_members_count")
    def members_count(self, obj: models.Organization) -> int:
        return t.cast(int, getattr(obj, "_members_count", 0))

    @admin.display(description="Events", ordering="_events_count")
    def events_count(self, obj: models.Organization) -> int:
        return t.cast(int, getattr(obj, "_events_count", 0))

    @admin.display(description="Stripe", boolean=True)
    def stripe_connected(self, obj: models.Organization) -> bool:
        return obj.is_stripe_connected


@admin.register(models.OrganizationQuestionnaire)
class OrganizationQuestionnaireAdmin(ModelAdmin, OrganizationLinkMixin):  # type: ignore[misc]
    list_display = ["__str__", "organization_link", "questionnaire_link"]
    autocomplete_fields = ["organization", "questionnaire"]
    filter_horizontal = ["event_series", "events"]

    def questionnaire_link(self, obj: models.OrganizationQuestionnaire) -> str:
        url = reverse("admin:questionnaires_questionnaire_change", args=[obj.questionnaire.id])
        return format_html('<a href="{}">{}</a>', url, obj.questionnaire.name)

    questionnaire_link.short_description = "Questionnaire"  # type: ignore[attr-defined]


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
        colors: dict[t.Any, str] = {
            models.OrganizationMembershipRequest.Status.PENDING: "orange",
            models.OrganizationMembershipRequest.Status.APPROVED: "green",
            models.OrganizationMembershipRequest.Status.REJECTED: "red",
        }
        color = colors.get(obj.status, "gray")
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
