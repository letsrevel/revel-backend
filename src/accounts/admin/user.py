"""Admin interface for accounts app.

This module provides comprehensive admin interfaces for all accounts models
including user management, authentication, and security features.
"""

import typing as t

import structlog
from django import forms
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import AdminPasswordChangeForm, UserChangeForm
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Exists, IntegerField, OuterRef, QuerySet, Subquery
from django.db.models.functions import Coalesce
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, QueryDict
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext
from django_google_sso.admin import GoogleSSOInlineAdmin, get_current_user_and_admin
from unfold.admin import ModelAdmin, TabularInline

from accounts.admin import formatters
from accounts.admin.user_extras import (
    ExternalIdentityInline,
    GlobalBanInline,
    IsOrganizationOwnerFilter,
    ReferralCodeInline,
)
from accounts.jwt import blacklist_user_tokens
from accounts.models import (
    DietaryRestriction,
    EmailVerificationReminderTracking,
    GlobalBan,
    RevelUser,
    UserBillingProfile,
    UserDataExport,
    UserDietaryPreference,
)
from accounts.service.account import convert_guest, request_password_reset, send_verification_email_for_user
from accounts.service.global_ban_service import is_email_globally_banned
from accounts.service.impersonation import can_impersonate, create_impersonation_request
from accounts.tasks.gdpr import account_deletion_dispatch
from accounts.utils.email_normalization import normalize_email_for_matching
from common.client_ip import get_client_ip
from common.models import SiteSettings
from events.models import GeneralUserPreferences

logger = structlog.get_logger(__name__)

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


class UserBillingProfileInline(TabularInline):  # type: ignore[misc]
    """Inline for user billing profile."""

    model = UserBillingProfile
    extra = 0
    can_delete = False
    readonly_fields = [
        "billing_name",
        "vat_id",
        "vat_country_code",
        "vat_id_validated",
        "vat_id_validated_at",
        "vies_request_identifier",
        "billing_address",
        "billing_email",
        "self_billing_agreed",
    ]
    fields = readonly_fields


def _queue_account_erasure(user_ids: list[t.Any]) -> None:
    """Deactivate the accounts in this transaction, then queue their erasure for after commit.

    It clears the self-service mark too: an admin deletion that fails leaves the account inactive.
    """
    RevelUser.objects.filter(pk__in=user_ids).update(is_active=False, deletion_requested_at=None)
    for user_id in user_ids:
        # The callback logs its own failure; robust is a backstop so one callback can never skip the others.
        transaction.on_commit(account_deletion_dispatch(str(user_id)), robust=True)


def _is_active_changed_by_admin(form: t.Any, obj: RevelUser, stored_active: bool) -> bool:
    """Whether the admin changed ``is_active``: against the value the form showed, else against the row."""
    if "is_active" not in form.fields:  # read-only or not in the form
        return False
    if form["is_active"].html_initial_name in form.data:
        return "is_active" in form.changed_data
    return bool(obj.is_active != stored_active)


def _lock_and_refuse_erased(pk: t.Any) -> None:
    """Lock the account row and raise a form error when the account is erased or deleted.

    The caller's transaction keeps the lock until its save. The deletion task locks the row first too, so a
    post during a running erasure waits for it and is then refused (also when the task deleted the row).
    """
    try:
        erased_at = RevelUser.objects.select_for_update().values_list("erased_at", flat=True).get(pk=pk)
    except RevelUser.DoesNotExist:  # a full delete committed while this post waited for the lock
        raise ValidationError(_("This account was deleted."), code="deleted") from None
    if erased_at is not None:
        raise ValidationError(_("This account was erased. An erased account cannot be edited."), code="erased")


_PICTURE_FILES = ("profile_picture", "profile_picture_thumbnail", "profile_picture_preview")


def _replace_picture_files_after_commit(user: RevelUser, old_files: list[str]) -> None:
    """After commit, delete the old picture files and make the derived images of the new picture."""
    from common.thumbnails.service import get_thumbnail_path
    from common.thumbnails.tasks import delete_orphaned_thumbnails_task, generate_thumbnails_task

    user_id = str(user.pk)
    new_name = user.profile_picture.name
    # A derived path depends on the source stem only: the new picture can reuse the old derived paths.
    new_files = {new_name, *(get_thumbnail_path(new_name, name) for name in _PICTURE_FILES[1:])} if new_name else set()
    old_files = [name for name in old_files if name not in new_files]
    if old_files:
        # robust: a broker failure must not fail the saved change; the old files then stay (harmless).
        transaction.on_commit(lambda: delete_orphaned_thumbnails_task.delay(thumbnail_paths=old_files), robust=True)
    if user.profile_picture:
        transaction.on_commit(
            lambda: generate_thumbnails_task.delay(
                app="accounts", model="reveluser", pk=user_id, field="profile_picture"
            ),
            robust=True,
        )


class RevelUserChangeForm(UserChangeForm):  # type: ignore[type-arg]
    """The change form refuses a post for an erased account, for example from a page opened before the erasure.

    Such a page still holds the old email, names and profile, and a save would write them back onto the row.
    ``erased_at`` is read under the row lock that ``save_model`` also takes: the admin change view runs in one
    transaction, so an erasure cannot commit between this check and the save.

    The page carries the value that it showed for each field (a hidden initial value). Django reloads the row
    when the form is posted, so without it a field that changed in the database after the page was opened
    counts as changed by the admin, and the save would write the old value back. A post without these values
    (a scripted client) is compared with the row that the view loaded. A file field always compares the post
    only, so it needs no shown value.
    """

    def __init__(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Show the hidden initial values on the page, and use them when the post carries them."""
        super().__init__(*args, **kwargs)
        # A rendered page sends a shown value for every field, except an empty many-to-many field: its hidden
        # widget renders no input. So any shown value in the post means "the page showed it", empty included.
        prefix = self.add_initial_prefix("")
        rendered = not self.is_bound or any(key.startswith(prefix) for key in self.data)
        if self.is_bound and rendered:
            self._keep_empty_shown_relations()
        for field in self.fields.values():
            if not field.disabled and not isinstance(field, forms.FileField):
                field.show_hidden_initial = rendered

    def _keep_empty_shown_relations(self) -> None:
        """Put the empty shown value of a relation into the post.

        A page rendered again after a validation error takes the shown value from the post when the post has
        it, else from the submitted value. Without the key, the submitted relations would become the shown
        value, and the next post would not save them.
        """
        missing = [
            self.add_initial_prefix(name)
            for name, field in self.fields.items()
            if isinstance(field.hidden_widget(), forms.MultipleHiddenInput)
            and self.add_initial_prefix(name) not in self.data
        ]
        if not missing:
            return
        if isinstance(self.data, QueryDict):
            data = self.data.copy()
            for key in missing:
                data.setlist(key, [])
            self.data = data
        else:
            self.data = {**self.data, **dict.fromkeys(missing, [])}

    def clean(self) -> dict[str, t.Any] | None:
        cleaned_data = super().clean()
        _lock_and_refuse_erased(self.instance.pk)
        return cleaned_data


class RevelAdminPasswordChangeForm(AdminPasswordChangeForm):  # type: ignore[type-arg]
    """The admin password change writes only the password, and refuses an erased or deleted account.

    Like a password reset, it sets ``credentials_changed_at`` and ends every session of the account.
    The view loads the account before the post is checked, so the instance can be stale: a full save would
    write back every other field, for example the personal data that an erasure removed meanwhile.
    ``user_change_password`` runs the check and the save in one transaction, under the row lock. The error
    goes on ``password1``: the password page shows no errors that are not on a field.
    """

    def clean(self) -> dict[str, t.Any] | None:
        cleaned_data = super().clean()
        try:
            _lock_and_refuse_erased(self.user.pk)
        except ValidationError as error:
            self.add_error("password1", error)
        return cleaned_data

    def save(self, commit: bool = True) -> t.Any:
        user = super().save(commit=False)
        if commit:
            user.credentials_changed_at = timezone.now()
            user.save(update_fields=["password", "credentials_changed_at"])
            blacklist_user_tokens(user)
        return user


@admin.register(RevelUser)
class RevelUserAdmin(UserAdmin, ModelAdmin):  # type: ignore[type-arg,misc]
    """Enhanced admin for RevelUser with comprehensive user management."""

    # List view configuration
    list_display = [
        "profile_thumbnail_display",
        "display_name_display",
        "email_link",
        "pronouns",
        "email_verified_display",
        "totp_active_display",
        "is_staff",
        "is_active",
        "guest",
        "erased_display",
        "date_joined",
        "event_count",
        "organization_count",
        "impersonate_link",
    ]
    list_display_links = ["profile_thumbnail_display", "display_name_display"]
    list_filter = [
        IsOrganizationOwnerFilter,
        "is_staff",
        "is_superuser",
        "is_active",
        "email_verified",
        "totp_active",
        "guest",
        ("erased_at", admin.EmptyFieldListFilter),
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
        "guest",  # changed only through the "Convert guest" action (convert_guest)
        "erased_at",  # set only by the account erasure (#1476)
        "deletion_requested_at",  # set only by the self-service deletion confirm (#1476)
        "date_joined",
        "last_login",
        "display_name_display",
        "profile_image_preview_display",
        "event_participation_display",
        "organization_participation_display",
        "stripe_account_id",
        "stripe_account_email",
        "stripe_charges_enabled",
        "stripe_details_submitted",
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
                    ("deletion_requested_at", "erased_at"),
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
            "Stripe Connect",
            {
                "fields": (
                    "stripe_account_id",
                    "stripe_account_email",
                    ("stripe_charges_enabled", "stripe_details_submitted"),
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
        UserBillingProfileInline,
        ReferralCodeInline,
        GlobalBanInline,
        ExternalIdentityInline,
        DietaryRestrictionInline,
        UserDietaryPreferenceInline,
        UserDataExportInline,
        EmailVerificationReminderTrackingInline,
    ]

    # Autocomplete
    autocomplete_fields = ["groups"]

    form = RevelUserChangeForm
    change_password_form = RevelAdminPasswordChangeForm

    def user_change_password(self, request: HttpRequest, id: str, form_url: str = "") -> HttpResponse:
        """Run the inherited view in one transaction: the row lock of the form check then covers the save."""
        with transaction.atomic():
            return super().user_change_password(request, id, form_url)

    def save_model(self, request: HttpRequest, obj: RevelUser, form: t.Any, change: bool) -> None:
        """An admin who changes ``is_active`` clears the self-service deletion mark: the admin decides now.

        Without the mark, a pending self-service deletion that fails does not change ``is_active`` again. The
        emailed links refuse a marked account, so an operator who reactivates one by hand clears it here.
        An edit that does not change ``is_active`` keeps the stored ``is_active`` and mark, read under the row
        lock that a deletion confirm also takes, so a form opened before the confirm does not write over them.

        The save writes only the fields that the admin changed. The other fields of ``obj`` can be older than
        the row (from the page, or loaded before the row lock), so they are read again under the lock.

        A new or removed picture also clears the derived images. The old files are deleted after commit, and
        a new picture gets its derived images after commit, as an upload through the API does.
        """
        if not change:
            super().save_model(request, obj, form, change)
            return
        stored_active, *stored_files = (
            RevelUser.objects.select_for_update().values_list("is_active", *_PICTURE_FILES).get(pk=obj.pk)
        )
        concrete = {field.name for field in obj._meta.concrete_fields}
        update_fields = {name for name in form.changed_data if name in concrete} - {"is_active"}
        if _is_active_changed_by_admin(form, obj, stored_active):
            obj.deletion_requested_at = None
            update_fields |= {"is_active", "deletion_requested_at"}
        picture_changed = "profile_picture" in update_fields
        if picture_changed:
            update_fields |= set(_PICTURE_FILES)
        # The save signals and the admin log then see the stored values of the fields that the admin did not change.
        obj.refresh_from_db(fields=sorted(concrete - update_fields - {obj._meta.pk.name}))
        if picture_changed:
            obj.profile_picture_thumbnail = obj.profile_picture_preview = None
        obj.save(update_fields=sorted(update_fields))
        if picture_changed:
            _replace_picture_files_after_commit(obj, [name for name in stored_files if name])

    def save_related(self, request: HttpRequest, form: t.Any, formsets: t.Any, change: bool) -> None:
        """On a change, set only the relations that the admin changed, like the fields in :meth:`save_model`."""
        if not change:
            super().save_related(request, form, formsets, change)
            return
        for field in form.instance._meta.many_to_many:
            if field.name in form.changed_data:
                field.save_form_data(form.instance, form.cleaned_data[field.name])
        for formset in formsets:
            self.save_formset(request, form, formset, change=change)

    # Actions
    def delete_model(self, request: HttpRequest, obj: RevelUser) -> None:
        """Deactivate the account now and queue its erasure after commit, like the user's own account deletion.

        The erasure gives back unfinished checkouts, cancels live subscriptions and purges history, and it must
        run outside a transaction. The deactivation stops sign-in until the task has removed the account.
        Accounts with referral rows are still refused by the delete confirmation (``PROTECT``); delete those
        through the account deletion flow.
        """
        _queue_account_erasure([obj.pk])

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet[RevelUser]) -> None:
        """Bulk delete: deactivate and queue the erasure of each selected account (see :meth:`delete_model`)."""
        _queue_account_erasure(list(queryset.values_list("pk", flat=True)))

    actions = [
        "send_verification_email",
        "send_password_reset_email",
        "ban_selected_users",
        "clear_stripe_connect_account",
        "convert_guests",
        "force_full_delete",
    ]

    def has_superuser_permission(self, request: HttpRequest) -> bool:
        """Gate for superuser-only actions (``@admin.action(permissions=["superuser"])``)."""
        return bool(request.user.is_superuser)

    @admin.action(description=_("Force full delete of erased accounts (superuser only)"), permissions=["superuser"])
    def force_full_delete(self, request: HttpRequest, queryset: QuerySet[RevelUser]) -> TemplateResponse | None:
        """Delete erased accounts and their kept tickets and passes now, before retention ends (#1476).

        An erased account keeps its row while it holds tickets or passes (see ``delete_user_account``).
        This action runs the normal full delete instead, after a confirmation page. Accounts that are not
        erased are skipped: delete them with the normal delete. Each queued deletion is logged with the
        actor id and the account id only.
        """
        if not request.user.is_superuser:  # the action is hidden from others; this is a backstop
            self.message_user(request, _("Superuser required."), level=messages.ERROR)
            return None
        erased = list(queryset.filter(erased_at__isnull=False))
        if not erased:
            self.message_user(request, _("No erased account selected."), level=messages.WARNING)
            return None
        if request.POST.get("confirm_force_full_delete") != "yes":
            context = {
                **self.admin_site.each_context(request),
                "title": _("Force full delete"),
                "opts": self.model._meta,
                "accounts": erased,
                "skipped": queryset.count() - len(erased),
                "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
            }
            return TemplateResponse(request, "admin/accounts/force_full_delete_confirm.html", context)
        RevelUser.objects.filter(pk__in=[account.pk for account in erased]).update(deletion_requested_at=None)
        for account in erased:
            logger.warning("account_force_full_delete_queued", actor_id=str(request.user.pk), user_id=str(account.pk))
            transaction.on_commit(account_deletion_dispatch(str(account.pk), force_full_delete=True), robust=True)
        self.message_user(
            request,
            ngettext(
                "Queued the full delete of %d erased account.",
                "Queued the full delete of %d erased accounts.",
                len(erased),
            )
            % len(erased),
            messages.SUCCESS,
        )
        return None

    @admin.action(description=_("Convert guest to full account"), permissions=["change"])
    def convert_guests(self, request: HttpRequest, queryset: QuerySet[RevelUser]) -> None:
        """Convert the selected guests through :func:`convert_guest` (non-guests are skipped).

        Marks the email verified and enrols referrals, so staff must only run this when ownership
        of the email is established. Globally banned guests are skipped and reported.
        """
        converted = 0
        banned = 0
        for guest in queryset.filter(guest=True):
            if is_email_globally_banned(guest.email):
                banned += 1
            elif convert_guest(guest):
                converted += 1
        self.message_user(
            request,
            ngettext("Converted %d guest to a full account.", "Converted %d guests to full accounts.", converted)
            % converted,
            messages.SUCCESS if converted else messages.WARNING,
        )
        if banned:
            self.message_user(
                request,
                ngettext(
                    "Skipped %d globally banned guest.",
                    "Skipped %d globally banned guests.",
                    banned,
                )
                % banned,
                messages.WARNING,
            )

    @admin.action(description="Clear stale Stripe Connect account (superuser only)")
    def clear_stripe_connect_account(self, request: HttpRequest, queryset: QuerySet[RevelUser]) -> None:
        """Detach a stale Connect account so the referrer can re-onboard from scratch.

        Nothing is deleted at Stripe — this only drops the local link. Account
        status sync deliberately flags rather than clears the id (a misconfigured
        platform key would otherwise wipe every live connection), so an account
        that is genuinely gone at Stripe is unlinked here by a human.
        """
        if not request.user.is_superuser:
            self.message_user(request, "Superuser required.", level=messages.ERROR)
            return
        cleared = 0
        for target_user in queryset.exclude(stripe_account_id__isnull=True):
            target_user.stripe_account_id = None
            target_user.stripe_charges_enabled = False
            target_user.stripe_details_submitted = False
            target_user.save(
                update_fields=target_user._stripe_update_fields(
                    "stripe_account_id", "stripe_charges_enabled", "stripe_details_submitted"
                )
            )
            cleared += 1
        self.message_user(request, f"Cleared the Stripe Connect account of {cleared} user(s).")

    @admin.action(description="Send verification email", permissions=["change"])
    def send_verification_email(self, request: HttpRequest, queryset: QuerySet[RevelUser]) -> None:
        """Send verification email to selected users."""
        sent_count = 0
        skipped_count = 0
        for user in queryset:
            # A verified account needs no link; an erased or pending-deletion one would get a refused link.
            if user.email_verified or user.erased_at or user.deletion_requested_at:
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

    @admin.action(description="Send password reset email", permissions=["change"])
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

    @admin.action(description=_("Ban selected users (global ban by email)"), permissions=["change"])
    def ban_selected_users(self, request: HttpRequest, queryset: QuerySet[RevelUser]) -> None:
        """Create global email bans for selected users, deactivating them."""
        banned_count = 0
        skipped_count = 0
        for target_user in queryset:
            if target_user.is_superuser or target_user.is_staff:
                skipped_count += 1
                continue
            _ban, created = GlobalBan.objects.get_or_create(
                ban_type=GlobalBan.BanType.EMAIL,
                normalized_value=normalize_email_for_matching(target_user.email),
                defaults={
                    "value": target_user.email,
                    "reason": str(_("Violation of terms of service")),
                    "created_by": t.cast(RevelUser, request.user),
                    "user": target_user,
                },
            )
            if created:
                banned_count += 1
            else:
                skipped_count += 1

        if banned_count:
            self.message_user(
                request,
                ngettext(
                    "Banned %d user.",
                    "Banned %d users.",
                    banned_count,
                )
                % banned_count,
                messages.SUCCESS,
            )
        if skipped_count:
            self.message_user(
                request,
                ngettext(
                    "Skipped %d user (staff/superuser or already banned).",
                    "Skipped %d users (staff/superuser or already banned).",
                    skipped_count,
                )
                % skipped_count,
                messages.WARNING,
            )

    # Custom displays (rendering logic lives in accounts/admin/formatters.py)
    @admin.display(description="")
    def profile_thumbnail_display(self, obj: RevelUser) -> str:
        """Display profile picture thumbnail in list view."""
        return formatters.profile_thumbnail(obj)

    @admin.display(description="Profile Picture Preview")
    def profile_image_preview_display(self, obj: RevelUser) -> str:
        """Display profile picture preview in detail view."""
        return formatters.profile_image_preview(obj)

    @admin.display(description="Name", ordering="preferred_name")
    def display_name_display(self, obj: RevelUser) -> str:
        name = obj.get_display_name()
        # Crown marks organization owners (see _is_owner annotation in get_queryset).
        if getattr(obj, "_is_owner", False):
            return f"👑 {name}"
        return name

    @admin.display(description="Email", ordering="email")
    def email_link(self, obj: RevelUser) -> str:
        return format_html('<a href="mailto:{}">{}</a>', obj.email, obj.email)

    @admin.display(description=_("Erased"), boolean=True, ordering="erased_at")
    def erased_display(self, obj: RevelUser) -> bool:
        return obj.erased_at is not None

    @admin.display(description="Email Verified", boolean=True)
    def email_verified_display(self, obj: RevelUser) -> bool:
        return obj.email_verified

    @admin.display(description="2FA Active", boolean=True)
    def totp_active_display(self, obj: RevelUser) -> bool:
        return obj.totp_active

    def get_queryset(self, request: HttpRequest) -> QuerySet[RevelUser]:
        """Annotate event/organization counts to avoid per-row queries on the changelist."""
        from events.models import EventRSVP, Organization, OrganizationMember, OrganizationStaff, Ticket

        def sub(qs: QuerySet[t.Any]) -> Coalesce:
            return Coalesce(Subquery(qs.values("c"), output_field=IntegerField()), 0)

        ticket_events = (
            Ticket.objects.filter(user=OuterRef("pk"))
            .order_by()
            .values("user")
            .annotate(c=Count("event", distinct=True))
        )
        rsvp_events = (
            EventRSVP.objects.filter(user=OuterRef("pk"))
            .order_by()
            .values("user")
            .annotate(c=Count("event", distinct=True))
        )
        owned = Organization.objects.filter(owner=OuterRef("pk")).order_by().values("owner").annotate(c=Count("pk"))
        member = (
            OrganizationMember.objects.filter(user=OuterRef("pk")).order_by().values("user").annotate(c=Count("pk"))
        )
        staff = OrganizationStaff.objects.filter(user=OuterRef("pk")).order_by().values("user").annotate(c=Count("pk"))

        qs: QuerySet[RevelUser] = super().get_queryset(request)
        return t.cast(
            QuerySet[RevelUser],
            qs.annotate(
                _event_count=sub(ticket_events) + sub(rsvp_events),
                _organization_count=sub(owned) + sub(member) + sub(staff),
                _is_owner=Exists(Organization.objects.filter(owner=OuterRef("pk"))),
            ),
        )

    @admin.display(description="Events", ordering="_event_count")
    def event_count(self, obj: RevelUser) -> int:
        return int(getattr(obj, "_event_count", 0))

    @admin.display(description="Organizations", ordering="_organization_count")
    def organization_count(self, obj: RevelUser) -> int:
        return int(getattr(obj, "_organization_count", 0))

    @admin.display(description="Event Participation")
    def event_participation_display(self, obj: RevelUser) -> str:
        """Display user's event participation details."""
        return formatters.event_participation(obj)

    @admin.display(description="Organization Participation")
    def organization_participation_display(self, obj: RevelUser) -> str:
        """Display user's organization participation details."""
        return formatters.organization_participation(obj)

    def get_list_display(self, request: HttpRequest) -> list[str]:  # type: ignore[override]
        list_display = list(super().get_list_display(request))
        if not request.user.is_superuser and "impersonate_link" in list_display:
            list_display.remove("impersonate_link")
        return list_display  # type: ignore[return-value]

    # Impersonation functionality
    @admin.display(description=_("Impersonate"))
    def impersonate_link(self, obj: RevelUser) -> str:
        """Display impersonation link for eligible users."""
        return formatters.impersonate_link(obj)

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
            ip_address = get_client_ip(request) or None
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


@admin.register(UserDataExport)
class UserDataExportAdmin(ModelAdmin):  # type: ignore[misc]
    """Admin for user data exports with GDPR compliance features."""

    list_display = ["user_link", "status", "status_display", "completed_at", "file_size", "created_at"]
    list_select_related = ["user"]
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
            UserDataExport.UserDataExportStatus.PENDING: "orange",
            UserDataExport.UserDataExportStatus.PROCESSING: "blue",
            UserDataExport.UserDataExportStatus.READY: "green",
            UserDataExport.UserDataExportStatus.FAILED: "red",
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
