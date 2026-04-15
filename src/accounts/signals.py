"""Signal handlers for account-related operations."""

import structlog
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from accounts.models import GlobalBan, RevelUser
from accounts.tasks import notify_admin_new_user_joined, notify_admin_new_user_joined_discord
from common.models import SiteSettings

logger = structlog.get_logger(__name__)


@receiver(post_save, sender=RevelUser)
def notify_admin_on_user_creation(
    sender: type[RevelUser], instance: RevelUser, created: bool, **kwargs: object
) -> None:
    """Send admin notifications when a new user joins.

    Standalone notification system that runs alongside the main notification
    system. Checks SiteSettings.notify_user_joined and dispatches Celery tasks
    to send Pushover and Discord notifications if enabled. The Discord task
    is only dispatched for non-guest users; the generic "new user" message
    counts non-guest users only, so firing it for a guest signup would show
    an unchanged count.

    Args:
        sender: The model class (RevelUser)
        instance: The actual user instance being saved
        created: True if this is a new user
        **kwargs: Additional keyword arguments
    """
    if not created:
        return

    # Check if admin notifications for new users are enabled
    site_settings = SiteSettings.get_solo()
    if not site_settings.notify_user_joined:
        logger.debug(
            "user_joined_notification_disabled",
            user_id=str(instance.id),
            user_email=instance.email,
        )
        return

    user_id = str(instance.id)
    user_email = instance.email
    is_guest = instance.guest

    def _dispatch() -> None:
        notify_admin_new_user_joined.delay(user_id=user_id, user_email=user_email, is_guest=is_guest)
        if not is_guest:
            notify_admin_new_user_joined_discord.delay()

    transaction.on_commit(_dispatch)

    logger.info(
        "user_joined_notification_dispatched",
        user_id=user_id,
        user_email=user_email,
        is_guest=is_guest,
    )


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
