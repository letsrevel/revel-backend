"""Admin for referral program applications and invites.

Decisions happen on the change form via Unfold submit-line buttons (Approve / Reject /
Reject permanently); the form is saved first so an edited code, percent or admin note is
what the service uses. Rows are created by the public endpoint or the Invite page.
"""

import typing as t

from django import forms
from django.contrib import admin, messages
from django.db.models import Case, IntegerField, QuerySet, Value, When
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import action
from unfold.enums import ActionVariant

from accounts.exceptions import ReferralApplicationConflictError, ReferralApplicationError
from accounts.models import ReferralApplication, RevelUser
from accounts.service import referral_application_service

PENDING = ReferralApplication.Status.PENDING


class ReferralApplicationForm(forms.ModelForm):  # type: ignore[type-arg]
    """Change form: validates an edited code against live codes and applications."""

    class Meta:
        model = ReferralApplication
        fields = ["code", "revenue_share_percent", "admin_note"]

    def clean_code(self) -> str:
        code = t.cast(str, self.cleaned_data["code"])
        try:
            referral_application_service.assert_code_available(code, exclude_pk=self.instance.pk)
        except ReferralApplicationConflictError as exc:
            raise forms.ValidationError(str(exc)) from exc
        return code


class EnrolledFilter(admin.SimpleListFilter):
    """Approved-and-enrolled vs approved-but-not-yet-signed-up."""

    title = _("enrolled")
    parameter_name = "enrolled"

    def lookups(self, request: HttpRequest, model_admin: t.Any) -> list[tuple[str, str]]:
        return [("yes", str(_("Yes"))), ("no", str(_("No")))]

    def queryset(self, request: HttpRequest, queryset: QuerySet[ReferralApplication]) -> QuerySet[ReferralApplication]:
        if self.value() == "yes":
            return queryset.filter(user__isnull=False)
        if self.value() == "no":
            return queryset.filter(user__isnull=True)
        return queryset


@admin.register(ReferralApplication)
class ReferralApplicationAdmin(ModelAdmin):  # type: ignore[misc]
    """Referral program applications and invites."""

    form = ReferralApplicationForm
    list_display = [
        "email",
        "code",
        "revenue_share_percent",
        "status",
        "source",
        "enrolled",
        "created_at",
        "decided_at",
    ]
    list_filter = ["status", "source", EnrolledFilter]
    search_fields = ["email", "code", "note"]
    list_select_related = ["user", "decided_by"]
    ordering = ["-created_at"]
    actions_submit_line = ["approve", "reject", "block"]
    readonly_fields = ["email", "note", "source", "status", "decided_by", "decided_at", "user", "created_at"]
    fieldsets = [
        (None, {"fields": ("email", "status", "source", "user")}),
        (_("Decision"), {"fields": ("code", "revenue_share_percent", "admin_note", "decided_by", "decided_at")}),
        (_("Application"), {"fields": ("note", "created_at")}),
    ]

    def get_queryset(self, request: HttpRequest) -> QuerySet[ReferralApplication]:
        """Pending rows first, then newest."""
        return (
            super()
            .get_queryset(request)
            .annotate(
                _pending_first=Case(When(status=PENDING, then=Value(0)), default=Value(1), output_field=IntegerField())
            )
            .order_by("_pending_first", "-created_at")
        )

    @admin.display(description=_("Enrolled"), boolean=True)
    def enrolled(self, obj: ReferralApplication) -> bool:
        return obj.user_id is not None

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Rows come from the public endpoint or the Invite page."""
        return False

    def get_readonly_fields(self, request: HttpRequest, obj: t.Any = None) -> list[str]:
        """Lock the decision fields once a decision has been made."""
        base = list(self.readonly_fields)
        if obj is not None and obj.status != PENDING:
            base += ["code", "revenue_share_percent", "admin_note"]
        return base

    def get_actions_submit_line(self, request: HttpRequest, object_id: t.Any = None) -> list[t.Any]:
        """Decision buttons only make sense while the row is pending."""
        if object_id is None:
            return []
        obj = ReferralApplication.objects.filter(pk=object_id).only("status").first()
        if obj is None or obj.status != PENDING:
            return []
        return list(super().get_actions_submit_line(request, object_id))

    def _decide(self, request: HttpRequest, label: str, fn: t.Callable[[], ReferralApplication]) -> None:
        try:
            application = fn()
        except ReferralApplicationError as exc:
            self.message_user(request, str(exc), messages.ERROR)
            return
        suffix = (
            _("enrolled immediately (account exists).")
            if application.user_id is not None and application.status == ReferralApplication.Status.APPROVED
            else _("email sent.")
        )
        self.message_user(request, f"{label} — {suffix}", messages.SUCCESS)

    @action(description=_("Approve"), permissions=["change"], icon="check", variant=ActionVariant.SUCCESS)
    def approve(self, request: HttpRequest, obj: ReferralApplication) -> None:
        actor = t.cast(RevelUser, request.user)
        self._decide(request, str(_("Approved")), lambda: referral_application_service.approve(obj, actor=actor))

    @action(description=_("Reject"), permissions=["change"], icon="close", variant=ActionVariant.WARNING)
    def reject(self, request: HttpRequest, obj: ReferralApplication) -> None:
        actor = t.cast(RevelUser, request.user)
        self._decide(
            request,
            str(_("Rejected")),
            lambda: referral_application_service.reject(obj, actor=actor, admin_note=obj.admin_note),
        )

    @action(description=_("Reject permanently"), permissions=["change"], icon="block", variant=ActionVariant.DANGER)
    def block(self, request: HttpRequest, obj: ReferralApplication) -> None:
        actor = t.cast(RevelUser, request.user)
        self._decide(
            request,
            str(_("Rejected permanently")),
            lambda: referral_application_service.block(obj, actor=actor, admin_note=obj.admin_note),
        )
