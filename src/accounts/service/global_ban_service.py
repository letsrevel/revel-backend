"""Service layer for global user banning."""

from datetime import datetime

import structlog
from django.db import transaction
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from accounts.jwt import blacklist_user_tokens
from accounts.models import GlobalBan, RevelUser
from accounts.utils.email_normalization import (
    extract_domain,
    normalize_email_for_matching,
    normalize_telegram_for_matching,
)

logger = structlog.get_logger(__name__)

BAN_ERROR_MESSAGE: str = _("Registration is not available.")  # type: ignore[assignment]


def email_ban_filter(email: str) -> Q:
    """The ``GlobalBan`` filter that matches a ban of ``email`` (by exact email or domain).

    Args:
        email: Email address to check.

    Returns:
        A ``Q`` for ``GlobalBan`` querysets.
    """
    normalized = normalize_email_for_matching(email)
    domain = extract_domain(email)
    return Q(ban_type=GlobalBan.BanType.EMAIL, normalized_value=normalized) | Q(
        ban_type=GlobalBan.BanType.DOMAIN, normalized_value=domain
    )


def is_email_globally_banned(email: str) -> bool:
    """Check if an email is globally banned (by exact email or domain).

    Args:
        email: Email address to check.

    Returns:
        True if the email or its domain is banned.
    """
    return GlobalBan.objects.filter(email_ban_filter(email)).exists()


def is_telegram_globally_banned(username: str) -> bool:
    """Check if a Telegram username is globally banned.

    Args:
        username: Telegram username to check.

    Returns:
        True if the username is banned.
    """
    normalized = normalize_telegram_for_matching(username)
    return GlobalBan.objects.filter(
        ban_type=GlobalBan.BanType.TELEGRAM,
        normalized_value=normalized,
    ).exists()


def deactivate_user_for_ban(user: RevelUser, reason: str) -> None:
    """Deactivate a user due to a global ban and send notification.

    Args:
        user: The user to deactivate.
        reason: Reason for the ban (included in notification).
    """
    if user.is_staff or user.is_superuser:
        logger.warning("ban_skipped_privileged_user", user_id=str(user.id), email=user.email)
        return

    if not user.is_active:
        # ``is_active`` was read earlier. A failed self-service deletion may have given the access back since
        # then: this UPDATE waits for its row lock and makes the account inactive again, and clears the mark
        # so that a later failure does not give the access back either.
        with transaction.atomic():
            pending = _has_deletion_mark(user)
            RevelUser.objects.filter(pk=user.pk).update(is_active=False, deletion_requested_at=None)
        _queue_deletion_of_pending_account(user, pending)
        blacklist_user_tokens(user)
        _deactivate_apps_owned_by(user)
        logger.info("user_already_inactive", user_id=str(user.id), email=user.email)
        return

    # Send notification before deactivation to ensure delivery channels can reach the user
    from notifications.enums import NotificationType
    from notifications.signals import notification_requested

    notification_requested.send(
        sender=GlobalBan,
        notification_type=NotificationType.ACCOUNT_BANNED,
        user=user,
        context={"ban_reason": reason},
    )

    with transaction.atomic():
        # ``user`` was read earlier: a deletion confirm may have marked the account since then.
        pending = _has_deletion_mark(user)
        user.is_active = False
        user.deletion_requested_at = None
        user.save(update_fields=["is_active", "deletion_requested_at"])
    _queue_deletion_of_pending_account(user, pending)

    # Blacklist all outstanding JWT tokens so the user is immediately locked out. This also
    # revokes every OAuth credential the user *granted* to other people's apps.
    blacklist_user_tokens(user)

    _deactivate_apps_owned_by(user)

    logger.warning("user_deactivated_for_ban", user_id=str(user.id), email=user.email, reason=reason)


def deactivate_restored_account_if_banned(user_id: str, mark: datetime) -> bool:
    """Undo the restore of a failed self-service deletion when the email of the account is banned.

    Call it after the restore has committed (see
    :func:`~accounts.service.account_erasure.restore_access_after_failed_deletion`). The account goes back to
    the state that a refused restore leaves: inactive, with ``mark`` (the mark that the restore cleared). So
    the deletion that the user asked for is not lost: the stale-mark sweep queues it again. One UPDATE does
    this, and only for a row that is still as the restore left it (active, no mark). The account gets no ban
    notification: it asked for its deletion. Return whether the restore was undone.

    The caller runs this as a robust on-commit callback, so an error here is only logged. An error before the
    UPDATE (the read, the ban lookup) leaves the account as restored: active, also when its email is banned,
    until the next ban of it or an admin action. An error after the UPDATE leaves the account inactive and
    pending; the token blacklist or the OAuth-app step can then be missing, and an inactive account cannot
    use a token.
    """
    user = RevelUser.objects.filter(pk=user_id, is_active=True, deletion_requested_at__isnull=True).first()
    if user is None or not is_email_globally_banned(user.email):
        return False
    as_restored = RevelUser.objects.filter(pk=user.pk, is_active=True, deletion_requested_at__isnull=True)
    if not as_restored.update(is_active=False, deletion_requested_at=mark):
        return False  # changed meanwhile: a ban task, an admin or a new confirmation decided
    logger.warning("account_restore_undone_for_ban", user_id=str(user.pk))
    blacklist_user_tokens(user)
    _deactivate_apps_owned_by(user)
    return True


def _has_deletion_mark(user: RevelUser) -> bool:
    """Lock the account row and read whether a self-service deletion is pending for it (it has the mark).

    Call it in the transaction that clears the mark: the row lock makes the read and the clear one step, so a
    deletion confirm commits before both or after both.
    """
    mark = RevelUser.objects.select_for_update().filter(pk=user.pk).values_list("deletion_requested_at", flat=True)
    return mark.first() is not None


def _queue_deletion_of_pending_account(user: RevelUser, pending: bool) -> None:
    """Queue the deletion that the user asked for, when the ban cleared the mark of a pending deletion.

    The run that the confirm queued stops when it finds its mark gone (``accounts.tasks.gdpr._mark_changed``).
    This run has no mark, as an admin run: it deletes or erases the account, and its failure never gives the
    access back to a banned account.
    """
    if not pending:
        return
    from accounts.tasks.gdpr import account_deletion_dispatch  # lazy: the tasks import this module

    transaction.on_commit(account_deletion_dispatch(str(user.pk)), robust=True)


def _deactivate_apps_owned_by(user: RevelUser) -> None:
    """The other direction (R-116): disarm the apps the banned user *owns*.

    Deliberately here and not in ``blacklist_user_tokens``, which email rotation also calls — deactivating a
    developer's apps because they changed their address would cut off all of that app's users, which is
    right for a ban and wrong for an email change.
    """
    from oauth.service import app_service  # lazy: oauth imports accounts

    deactivated, revoked = app_service.deactivate_apps_owned_by(user)
    if deactivated:
        logger.warning(
            "banned_user_oauth_apps_deactivated",
            user_id=str(user.id),
            apps=deactivated,
            credentials_revoked=revoked,
        )


@transaction.atomic
def process_email_ban(ban: GlobalBan) -> None:
    """Process an email ban: find and deactivate the matching user.

    Args:
        ban: The GlobalBan instance (EMAIL type).
    """
    normalized = ban.normalized_value or normalize_email_for_matching(ban.value)
    # Match by exact email or by normalized form (catches Gmail dot/plus variations).
    # NOTE: This lookup uses email__iexact, so it will NOT retroactively find users whose
    # stored email differs only by Gmail dot-variants (e.g., banning "firstlast@gmail.com"
    # won't match a user stored as "first.last@gmail.com"). This is acceptable because
    # the normalization check in is_email_globally_banned() still blocks future logins.
    user = RevelUser.objects.filter(Q(email__iexact=ban.value) | Q(email__iexact=normalized)).first()
    if not user:
        logger.info("email_ban_no_user_found", ban_id=str(ban.id), value=ban.value)
        return

    GlobalBan.objects.filter(id=ban.id).update(user=user)
    deactivate_user_for_ban(user, reason=ban.reason)
    logger.info("email_ban_processed", ban_id=str(ban.id), user_id=str(user.id))


@transaction.atomic
def process_telegram_ban(ban: GlobalBan) -> None:
    """Process a Telegram ban: find and deactivate the matching user.

    Args:
        ban: The GlobalBan instance (TELEGRAM type).
    """
    from telegram.models import TelegramUser

    normalized = normalize_telegram_for_matching(ban.value)
    tg_user = (
        TelegramUser.objects.filter(
            telegram_username__iexact=normalized,
            user__isnull=False,
        )
        .select_related("user")
        .first()
    )

    if not tg_user or not tg_user.user:
        logger.info("telegram_ban_no_user_found", ban_id=str(ban.id), value=ban.value)
        return

    user = tg_user.user
    GlobalBan.objects.filter(id=ban.id).update(user=user)
    deactivate_user_for_ban(user, reason=ban.reason)
    logger.info("telegram_ban_processed", ban_id=str(ban.id), user_id=str(user.id))


def process_domain_ban(ban: GlobalBan) -> int:
    """Process a domain ban: find and deactivate all users with matching email domain.

    Args:
        ban: The GlobalBan instance (DOMAIN type).

    Returns:
        Number of users deactivated. An owner that was already inactive only gets its OAuth apps disarmed
        and is not counted.
    """
    domain = ban.normalized_value
    users = (
        RevelUser.objects.filter(
            email__iendswith=f"@{domain}",
            is_active=True,
        )
        .exclude(is_staff=True)
        .exclude(is_superuser=True)
        .iterator()
    )
    count = 0
    for user in users:
        deactivate_user_for_ban(user, reason=ban.reason)
        count += 1

    # R-116 also holds for an owner that is already inactive (for example while its deletion is pending):
    # the apps would keep working for their other users. Only the apps change; the account row, its mark
    # and the count stay as they are (``restore_access_after_failed_deletion`` already refuses a banned email).
    inactive_owners = (
        RevelUser.objects.filter(email__iendswith=f"@{domain}", is_active=False, oauth_oauthapplication__is_active=True)
        .exclude(is_staff=True)
        .exclude(is_superuser=True)
        .distinct()
    )
    for owner in list(inactive_owners):
        _deactivate_apps_owned_by(owner)

    logger.info("domain_ban_processed", ban_id=str(ban.id), domain=domain, deactivated_count=count)
    return count
