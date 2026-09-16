"""Admin for referral program applications and invites.

Decisions happen on the change form via Unfold submit-line buttons (Approve / Reject /
Reject permanently); the form is saved first so an edited code, percent or admin note is
what the service uses. Rows are created by the public endpoint or the Invite page.
"""

import typing as t
from decimal import Decimal

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.db.models import Case, IntegerField, QuerySet, Value, When
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import action
from unfold.enums import ActionVariant
from unfold.widgets import (
    UnfoldAdminDecimalFieldWidget,
    UnfoldAdminEmailInputWidget,
    UnfoldAdminTextareaWidget,
    UnfoldAdminTextInputWidget,
)

from accounts.exceptions import ReferralApplicationConflictError, ReferralApplicationError
from accounts.models import REFERRAL_CODE_VALIDATOR, ReferralApplication, ReferralCode, RevelUser
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


class ReferralInviteForm(forms.Form):
    """Admin invite: email, code, percent (default from settings), optional personal note."""

    email = forms.EmailField(widget=UnfoldAdminEmailInputWidget())
    code = forms.CharField(
        max_length=20,
        validators=[REFERRAL_CODE_VALIDATOR],
        widget=UnfoldAdminTextInputWidget(),
        help_text=_("3 to 20 letters, digits, dashes or underscores. Case-insensitive."),
    )
    revenue_share_percent = forms.DecimalField(
        max_digits=5,
        decimal_places=2,
        min_value=Decimal("0"),
        max_value=Decimal("100"),
        initial=settings.DEFAULT_REFERRAL_SHARE_PERCENT,
        widget=UnfoldAdminDecimalFieldWidget(),
    )
    note = forms.CharField(
        required=False,
        max_length=2000,
        widget=UnfoldAdminTextareaWidget(),
        help_text=_("Optional. Included in the invite email."),
    )


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
    actions_submit_line = ["approve", "reject", "block"]
    actions_list = ["invite"]
    readonly_fields = ["email", "note", "source", "status", "decided_by", "decided_at", "user", "created_at"]
    fieldsets = [
        (None, {"fields": ("email", "status", "source", "user")}),
        (_("Decision"), {"fields": ("code", "revenue_share_percent", "admin_note", "decided_by", "decided_at")}),
        (_("Application"), {"fields": ("note", "created_at")}),
    ]

    def get_queryset(self, request: HttpRequest) -> QuerySet[ReferralApplication]:
        """Pending rows first, then newest.

        Built from the default manager rather than ``super().get_queryset(request)``:
        the base ``ModelAdmin.get_queryset`` eagerly applies ``self.get_ordering(request)``
        to the *unannotated* queryset, which would raise ``FieldError`` on ``_pending_first``
        before the annotation below exists.
        """
        qs: QuerySet[ReferralApplication] = self.model._default_manager.get_queryset()
        return qs.annotate(
            _pending_first=Case(When(status=PENDING, then=Value(0)), default=Value(1), output_field=IntegerField())
        ).order_by("_pending_first", "-created_at")

    def get_ordering(self, request: HttpRequest) -> list[str]:
        """Pending rows first, then newest (the annotation comes from get_queryset).

        ``ChangeList.get_ordering`` starts from this and *appends* the queryset's own
        ``order_by``, so a class-level ``ordering = ["-created_at"]`` would run first and
        make ``_pending_first`` merely break ties on a microsecond timestamp. Returning the
        annotation-based ordering here instead is what actually drives the changelist sort.
        """
        return ["_pending_first", "-created_at"]

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
        if application.user_id is None or application.status != ReferralApplication.Status.APPROVED:
            self.message_user(request, f"{label} — {_('email sent.')}", messages.SUCCESS)
            return
        suffix = str(_("enrolled immediately (account exists)."))
        # The row keeps showing the typed code even when an existing one was reactivated instead.
        kept = ReferralCode.objects.filter(user_id=application.user_id).values_list("code", flat=True).first()
        if kept is not None and kept.casefold() != application.code.casefold():
            suffix += " " + str(_("existing code {} kept; the typed code was ignored.").format(kept))
        self.message_user(request, f"{label} — {suffix}", messages.SUCCESS)

    @action(description=_("Approve"), permissions=["change"], icon="check", variant=ActionVariant.SUCCESS)  # type: ignore[untyped-decorator]
    def approve(self, request: HttpRequest, obj: ReferralApplication) -> None:
        actor = t.cast(RevelUser, request.user)
        self._decide(request, str(_("Approved")), lambda: referral_application_service.approve(obj, actor=actor))

    @action(description=_("Reject"), permissions=["change"], icon="close", variant=ActionVariant.WARNING)  # type: ignore[untyped-decorator]
    def reject(self, request: HttpRequest, obj: ReferralApplication) -> None:
        actor = t.cast(RevelUser, request.user)
        self._decide(
            request,
            str(_("Rejected")),
            lambda: referral_application_service.reject(obj, actor=actor, admin_note=obj.admin_note),
        )

    @action(description=_("Reject permanently"), permissions=["change"], icon="block", variant=ActionVariant.DANGER)  # type: ignore[untyped-decorator]
    def block(self, request: HttpRequest, obj: ReferralApplication) -> None:
        actor = t.cast(RevelUser, request.user)
        self._decide(
            request,
            str(_("Rejected permanently")),
            lambda: referral_application_service.block(obj, actor=actor, admin_note=obj.admin_note),
        )

    @action(description=_("Invite by email"), url_path="invite", permissions=["add_invite"], icon="person_add")  # type: ignore[untyped-decorator]
    def invite(self, request: HttpRequest) -> HttpResponse:
        """GET: render the invite form. POST: create the invite (or enroll) and open the row."""
        form = ReferralInviteForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            try:
                application = referral_application_service.create_invite(
                    email=form.cleaned_data["email"],
                    code=form.cleaned_data["code"],
                    revenue_share_percent=form.cleaned_data["revenue_share_percent"],
                    note=form.cleaned_data["note"],
                    actor=t.cast(RevelUser, request.user),
                )
            except ReferralApplicationError as exc:
                form.add_error(None, str(exc))
            else:
                sent = _("enrolled immediately (account exists).") if application.user_id else _("invite email sent.")
                self.message_user(request, f"{application.email}: {sent}", messages.SUCCESS)
                return HttpResponseRedirect(reverse("admin:accounts_referralapplication_change", args=[application.pk]))

        context = {
            **self.admin_site.each_context(request),
            "title": _("Invite to the referral program"),
            "opts": self.model._meta,
            "form": form,
        }
        return TemplateResponse(request, "admin/accounts/referral_invite.html", context)

    def has_add_invite_permission(self, request: HttpRequest) -> bool:
        """Invites reuse the model's change permission (add is disabled for the plain form)."""
        return request.user.has_perm("accounts.change_referralapplication")
