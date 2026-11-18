"""Signal handlers for account-related operations."""

import structlog
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from accounts.dispatch import guest_converted
from accounts.models import GlobalBan, RevelUser
from accounts.service import referral_application_service
from accounts.tasks import notify_admin_new_user_joined, notify_admin_new_user_joined_discord
from common.models import SiteSettings

logger = structlog.get_logger(__name__)


def _dispatch_user_joined_notifications(user: RevelUser, *, converted: bool = False) -> None:
    """Queue the admin notifications for ``user`` once the surrounding transaction commits.

    Guests only get Pushover (the Discord task counts registered users, which a guest does not change).
    """
    user_id = str(user.id)
    user_email = user.email
    is_guest = user.guest

    def _dispatch() -> None:
        notify_admin_new_user_joined.delay(
            user_id=user_id, user_email=user_email, is_guest=is_guest, converted=converted
        )
        if not is_guest:
            notify_admin_new_user_joined_discord.delay(is_guest=False)

    transaction.on_commit(_dispatch)
    logger.info(
        "user_joined_notification_dispatched",
        user_id=user_id,
        user_email=user_email,
        is_guest=is_guest,
        converted=converted,
    )


@receiver(post_save, sender=RevelUser)
def notify_admin_on_user_creation(
    sender: type[RevelUser], instance: RevelUser, created: bool, **kwargs: object
) -> None:
    """Send admin notifications when a new user joins.

    Standalone notification system that runs alongside the main notification
    system. Registered users are gated by SiteSettings.notify_user_joined (Pushover + Discord) and guest
    users by SiteSettings.notify_guest_user_joined (Pushover only).

    Args:
        sender: The model class (RevelUser)
        instance: The actual user instance being saved
        created: True if this is a new user
        **kwargs: Additional keyword arguments
    """
    if not created:
        return

    site_settings = SiteSettings.get_solo()
    enabled = site_settings.notify_guest_user_joined if instance.guest else site_settings.notify_user_joined
    if not enabled:
        logger.debug("user_joined_notification_disabled", user_id=str(instance.id), user_email=instance.email)
        return
    _dispatch_user_joined_notifications(instance)


@receiver(guest_converted)
def notify_admin_on_guest_conversion(sender: type[RevelUser], user: RevelUser, **kwargs: object) -> None:
    """A guest that becomes a registered user is a new registered user: notify like a signup."""
    if SiteSettings.get_solo().notify_user_joined:
        _dispatch_user_joined_notifications(user, converted=True)


@receiver(post_save, sender=RevelUser)
def enroll_referral_invitee_on_creation(
    sender: type[RevelUser], instance: RevelUser, created: bool, **kwargs: object
) -> None:
    """Turn an approved referral invite into a ReferralCode when the invitee signs up.

    Only verified signups count: guest accounts (created by guest checkout, possibly by somebody
    else typing the invitee's email) and accounts whose email nobody has verified yet are skipped,
    since anyone can type an invitee's address into the signup form. A guest is enrolled later,
    when they become a full user through ``account.convert_guest`` (password reset, OIDC login or
    the admin "Convert guest" action); an unverified signup when it verifies its email
    (``account.verify_email`` or a password reset).

    Delegates to :func:`try_enroll_invitee`, which never raises: a referral perk must not block
    account creation.
    """
    if not created or instance.guest or not instance.email_verified:
        return
    referral_application_service.try_enroll_invitee(instance)


@receiver(post_save, sender=GlobalBan)
def handle_global_ban_created(sender: type[GlobalBan], instance: GlobalBan, created: bool, **kwargs: object) -> None:
    """Enforce a global ban when it is first created.

    EMAIL and TELEGRAM bans are processed synchronously (single user).
    DOMAIN bans are dispatched to Celery (may affect many users).

    Args:
        sender: The model class (GlobalBan)
        instance: The ban instance
        created: True if this is a new ban
        **kwargs: Additional keyword arguments
    """
    if not created:
        return

    from accounts.service.global_ban_service import process_email_ban, process_telegram_ban

    if instance.ban_type == GlobalBan.BanType.DOMAIN:
        from accounts.tasks import process_domain_ban_task

        transaction.on_commit(lambda: process_domain_ban_task.delay(str(instance.id)))
        logger.info("domain_ban_task_dispatched", ban_id=str(instance.id), domain=instance.value)
    elif instance.ban_type == GlobalBan.BanType.EMAIL:
        process_email_ban(instance)
    elif instance.ban_type == GlobalBan.BanType.TELEGRAM:
        process_telegram_ban(instance)
