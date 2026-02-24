"""Service layer for global user banning."""

import structlog
from django.db import transaction
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from ninja_jwt.token_blacklist.models import BlacklistedToken, OutstandingToken

from accounts.models import GlobalBan, RevelUser
from accounts.utils.email_normalization import (
    extract_domain,
    normalize_email_for_matching,
    normalize_telegram_for_matching,
)

logger = structlog.get_logger(__name__)

BAN_ERROR_MESSAGE: str = _("Registration is not available.")  # type: ignore[assignment]


def _blacklist_user_tokens(user: RevelUser) -> int:
    """Blacklist all outstanding JWT tokens for a user.

    Args:
        user: The user whose tokens should be blacklisted.

    Returns:
        Number of tokens blacklisted.
    """
    outstanding = OutstandingToken.objects.filter(user=user).exclude(blacklistedtoken__isnull=False)
    count = 0
    for token in outstanding:
        BlacklistedToken.objects.get_or_create(token=token)
        count += 1
    if count:
        logger.info("user_tokens_blacklisted", user_id=str(user.id), count=count)
    return count


def is_email_globally_banned(email: str) -> bool:
    """Check if an email is globally banned (by exact email or domain).

    Args:
        email: Email address to check.

    Returns:
        True if the email or its domain is banned.
    """
    normalized = normalize_email_for_matching(email)
    domain = extract_domain(email)
    return GlobalBan.objects.filter(
        Q(ban_type=GlobalBan.BanType.EMAIL, normalized_value=normalized)
        | Q(ban_type=GlobalBan.BanType.DOMAIN, normalized_value=domain)
    ).exists()


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

    user.is_active = False
    user.save(update_fields=["is_active"])

    # Blacklist all outstanding JWT tokens so the user is immediately locked out
    _blacklist_user_tokens(user)

    logger.warning("user_deactivated_for_ban", user_id=str(user.id), email=user.email, reason=reason)


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
        Number of users deactivated.
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

    logger.info("domain_ban_processed", ban_id=str(ban.id), domain=domain, deactivated_count=count)
    return count
