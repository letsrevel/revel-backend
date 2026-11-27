"""Erase an account that still holds tickets or series passes: keep an anonymised row (#1476).

Deleting an account normally deletes the row, and the cascade takes its tickets with it. An account
with kept holdings (see ``events.service.account_deletion_service.kept_holdings``) keeps its row
instead, so that its tickets, passes, payments and refunds stay valid. Every other row that points
at the account gets the outcome that ``user.delete()`` would give it, and the account itself loses
every personal field. Only the holder name and email stay, on the kept tickets and passes, and the
billing snapshot on their payments (``buyer_billing_snapshot``; the raw Stripe payload is cleared).

The retention job (a later change) deletes the row when the last covered event ended 120 days ago.
"""

import typing as t
from datetime import datetime

import pyotp
import structlog
from django.core.files.storage import Storage
from django.db import transaction
from django.db.models import (
    CASCADE,
    DO_NOTHING,
    PROTECT,
    RESTRICT,
    SET_NULL,
    Exists,
    ForeignObjectRel,
    Model,
    ProtectedError,
    QuerySet,
)
from django.db.models.deletion import get_candidate_relations_to_delete
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.jwt import blacklist_user_tokens
from accounts.models import GlobalBan, RevelUser, UserDataExport
from accounts.service.global_ban_service import deactivate_restored_account_if_banned, email_ban_filter
from common.email_backend import erased_placeholder_email

if t.TYPE_CHECKING:
    from events.service.account_deletion_service import KeptHoldings

logger = structlog.get_logger(__name__)

_PROFILE_FILES = ("profile_picture", "profile_picture_thumbnail", "profile_picture_preview")


def _kept_rows(model: type[Model], field_name: str, kept: "KeptHoldings") -> dict[str, t.Any] | None:
    """The filter that selects the rows of ``model.field_name`` that stay; None when the relation keeps nothing.

    ``NotificationPreference`` stays, not silenced: the event-cancel audience skips silenced accounts, and the
    dispatcher already allows an erased account only the cancellation and refund emails.
    """
    from events.models import HeldSeriesPass, Payment, Ticket
    from notifications.models import NotificationPreference

    if field_name != "user":
        return None
    if model is Ticket:
        return {"pk__in": kept.ticket_ids}
    if model is HeldSeriesPass:
        return {"pk__in": kept.pass_ids}
    if model is Payment:
        return {"ticket_id__in": kept.ticket_ids}
    if model is NotificationPreference:
        return {}
    return None


def _related_querysets(user: RevelUser, kept: "KeptHoldings") -> t.Iterator[tuple[t.Any, str, QuerySet[t.Any]]]:
    """Each relation to ``user`` (hidden ones too, as ``user.delete()`` collects them) with its rows to act on.

    Tickets come first: a ``RESTRICT`` on ``Ticket.held_pass`` stops a pass delete while a ticket of it exists.
    """
    from events.models import Ticket

    candidates = t.cast(t.Iterable[ForeignObjectRel], get_candidate_relations_to_delete(RevelUser._meta))  # type: ignore[arg-type]
    relations = sorted(candidates, key=lambda rel: rel.related_model is not Ticket)
    for relation in relations:
        field = relation.field
        model = relation.related_model
        rows = model._base_manager.filter(**{field.name: user})
        keep = _kept_rows(model, field.name, kept)
        if keep is not None:
            rows = rows.exclude(**keep) if keep else rows.none()
        yield field.remote_field.on_delete, field.name, rows


def _detach_relations(user: RevelUser, kept: "KeptHoldings") -> None:
    """Give every row that points at ``user`` the outcome of ``user.delete()``, except the kept holdings."""
    for on_delete, field_name, rows in _related_querysets(user, kept):
        if on_delete is CASCADE:
            rows.delete()
        elif on_delete is SET_NULL:
            rows.update(**{field_name: None})
        elif on_delete in (PROTECT, RESTRICT):
            if rows.exists():
                raise ProtectedError(f"{rows.model._meta.label} rows protect the account from deletion.", set(rows))
        elif on_delete is not DO_NOTHING:  # SET_DEFAULT / SET(...): no relation uses them today
            raise TypeError(f"Unsupported on_delete on {rows.model._meta.label}: {on_delete!r}")


def erase_account_keeping_holdings(user: RevelUser, kept: "KeptHoldings") -> None:
    """Anonymise ``user`` in place and keep ``kept``. Run it inside the deletion transaction.

    Call it after ``release_capacity_of_account`` and ``kept_holdings`` (they lock and select the rows),
    and run ``gdpr.purge_user_history`` after it. The order matters:

    1. Copy the holder name and email onto the kept tickets and passes, and clear the stored Stripe
       payload (``raw_response``) of their payments. ``buyer_billing_snapshot`` stays.
    2. Blacklist the session tokens and revoke the OAuth tokens, before the cascade nulls their user.
    3. Keep the blacklist entries of the account as unlinked entries.
    4. Delete or null every other related row, as ``user.delete()`` would.
    5. Clear every personal field on the account, deactivate it and set ``erased_at``.

    Raises:
        RuntimeError: If a kept ticket or pass is gone after step 4 (a cascade reached it). The
            transaction rolls back, so the account stays as it was.
    """
    from events.models import HeldSeriesPass, Payment, Ticket
    from events.service.account_deletion_service import snapshot_holders
    from events.service.blacklist_retention import detach_blacklist_entries_of_account

    snapshot_holders(user, kept)
    # The stored Stripe payload of a kept payment holds the buyer's details; the amounts, VAT and
    # buyer_billing_snapshot stay, since a late invoice or credit note needs them.
    Payment.objects.filter(ticket_id__in=kept.ticket_ids).update(raw_response={})
    blacklist_user_tokens(user)
    detach_blacklist_entries_of_account(user)
    _detach_relations(user, kept)
    if Ticket.objects.filter(pk__in=kept.ticket_ids).count() != len(kept.ticket_ids) or HeldSeriesPass.objects.filter(
        pk__in=kept.pass_ids
    ).count() != len(kept.pass_ids):
        raise RuntimeError("A kept ticket or series pass was removed by the account erasure cascade.")
    _anonymise(user)
    logger.info(
        "account_erased_keeping_holdings",
        user_id=str(user.pk),
        kept_tickets=len(kept.ticket_ids),
        kept_passes=len(kept.pass_ids),
    )


def _anonymise(user: RevelUser) -> None:
    """Clear every personal field of ``user``, make it unable to sign in, and set ``erased_at``."""
    now = timezone.now()
    delete_profile_files_after_commit(user)
    for name in _PROFILE_FILES:
        setattr(user, name, None)
    user.username = user.email = erased_placeholder_email(user.pk.hex)
    user.first_name = user.last_name = user.preferred_name = user.pronouns = user.bio = ""
    user.phone_number = None
    user.stripe_account_id = None
    user.stripe_account_email = ""
    user.stripe_charges_enabled = user.stripe_details_submitted = False
    user.set_unusable_password()
    user.totp_secret = pyotp.random_base32()
    user.totp_active = False
    # Not verified: the placeholder is no address, and the save then links no register record to it.
    user.email_verified = False
    user.guest = False  # an inactive guest could otherwise be reactivated by guest conversion
    user.is_active = user.is_staff = user.is_superuser = False
    user.signup_contested_at = None
    user.last_login = None
    user.credentials_changed_at = now
    user.erased_at = now
    user.deletion_requested_at = None
    user.save(
        update_fields=[
            *_PROFILE_FILES,
            "username",
            "email",
            "email_verified",
            "guest",
            "first_name",
            "last_name",
            "preferred_name",
            "pronouns",
            "bio",
            "phone_number",
            "stripe_account_id",
            "stripe_account_email",
            "stripe_charges_enabled",
            "stripe_details_submitted",
            "password",
            "totp_secret",
            "totp_active",
            "is_active",
            "is_staff",
            "is_superuser",
            "signup_contested_at",
            "last_login",
            "credentials_changed_at",
            "erased_at",
            "deletion_requested_at",
        ]
    )


def delete_profile_files_after_commit(user: RevelUser) -> None:
    """After commit, delete the profile picture of ``user`` and its derived images from storage.

    Call it before the erasure clears the fields, or before ``user.delete()``: a row delete does not delete the
    files. The caller holds the account row lock, so the fields hold the stored files.
    """
    files = [(name, file.storage, file.name) for name in _PROFILE_FILES if (file := getattr(user, name))]
    if files:
        transaction.on_commit(_profile_file_cleanup(str(user.pk), files), robust=True)


def _profile_file_cleanup(user_id: str, files: list[tuple[str, Storage, str]]) -> t.Callable[[], None]:
    """A named on-commit callback that deletes the files of a deleted or erased account from storage.

    The delete runs after commit: a rollback restores the file references, but it cannot restore a deleted
    file. A failed delete is logged with the account id and the field name only (the file name holds the id
    too), the other files are still deleted, and a retrying task deletes the failed ones later.
    """

    def delete_erased_profile_files() -> None:
        failed: list[tuple[str, str]] = []
        for field_name, storage, file_name in files:
            try:
                storage.delete(file_name)
            except Exception:
                logger.error("account_erasure_profile_file_delete_failed", user_id=user_id, field=field_name)
                failed.append((field_name, file_name))
        if failed:
            from accounts.tasks.gdpr import retry_account_file_delete

            # A failed dispatch is logged by the robust on-commit handler; the error log above stays.
            retry_account_file_delete.delay(user_id=user_id, files=failed)

    return delete_erased_profile_files


def delete_data_export_file_after_commit(user: RevelUser) -> None:
    """After commit, delete the stored data-export ZIP of ``user``. Call it before the deletion removes the row.

    The export row goes with the account (CASCADE), on a full delete and on an erasure, but a row delete
    does not delete the file. The ZIP holds the whole profile, and its emailed link stays valid for 7 days.
    The caller holds the account row lock, so the export task cannot write a new file meanwhile.
    """
    export = UserDataExport.objects.filter(user=user).first()
    if export is not None and export.file and export.file.name:
        files = [("data_export", export.file.storage, export.file.name)]
        transaction.on_commit(_profile_file_cleanup(str(user.pk), files), robust=True)


def restore_access_after_failed_deletion(user_id: str, mark: datetime | None) -> bool:
    """Make the account active again when its self-service deletion failed for good; return whether it did.

    ``mark`` is the ``deletion_requested_at`` value that the failed run was queued with. Only a row that still
    has exactly that mark qualifies: ``confirm_account_deletion`` set it on an account that was active. A run
    that was queued without a mark (``None``) never restores, so it cannot clear the mark of a later request.
    An admin deletion or deactivation clears the mark, the stale-mark sweep replaces it, an erased row never
    qualifies, and neither does an account whose email is globally banned. One UPDATE re-checks all of this,
    so a change that commits after the read (an erasure, a ban, an email change) is not undone.

    The UPDATE cannot see a ban that commits while it runs, and a domain ban deactivates only the accounts
    that are active when its task reads them. So the ban is checked once more after the restore has committed
    (:func:`~accounts.service.global_ban_service.deactivate_restored_account_if_banned`), and a banned account
    goes back to the state that a refused restore leaves: inactive, with its mark. One of the two then
    sees the other: this check sees a ban that committed before it, and the task of a ban that commits later
    sees the account active. An email or Telegram ban writes the account row itself, so the row lock orders it
    with the UPDATE.
    """
    if mark is None:
        return False
    marked = RevelUser.objects.filter(pk=user_id, deletion_requested_at=mark, erased_at__isnull=True)
    email = marked.values_list("email", flat=True).first()
    if email is None:
        return False
    banned = GlobalBan.objects.filter(email_ban_filter(email))
    restored = bool(
        marked.filter(email=email).exclude(Exists(banned)).update(is_active=True, deletion_requested_at=None)
    )
    if restored:
        # After the commit of the UPDATE: at once in a worker (autocommit), else when the caller commits.
        # Robust: the caller is the failure handling of the deletion task, and an error of this check must not
        # replace the error of the deletion. Django logs it (ERROR) with the name of the callback.
        transaction.on_commit(_ban_check_after_restore(user_id, mark), robust=True)
    return restored


def _ban_check_after_restore(user_id: str, mark: datetime) -> t.Callable[[], None]:
    """A named on-commit callback: undo the restore when the email of the account is banned by now."""

    def check_ban_after_restore() -> None:
        deactivate_restored_account_if_banned(user_id, mark)

    return check_ban_after_restore


def lock_account_for_checkout(user: RevelUser) -> None:
    """Lock the account row of the buyer before a checkout writes a ticket, payment or pass for it.

    The deletion task takes the same row lock (NO KEY UPDATE) before it reads the account's holdings. So a
    checkout and a deletion of one account never overlap: the checkout commits first and the deletion then
    sees its rows, or the deletion commits first and the checkout is refused here. A foreign-key insert alone
    does not give this: Django's constraints are deferred, so the insert locks the account only at commit.

    Call it inside the checkout transaction, before the first tier, pass or seat lock: the account row is the
    first lock in the global order (see "Lock order" in ``docs/engineering-notes.md``).

    Raises:
        HttpError: 403 if the account is erased, deleted, or waits for its self-service deletion.
    """
    state = (
        RevelUser.objects.select_for_update(no_key=True)
        .filter(pk=user.pk)
        .values_list("erased_at", "deletion_requested_at")
        .first()
    )
    if state is None or any(state):
        raise HttpError(403, str(_("This account is being deleted.")))
