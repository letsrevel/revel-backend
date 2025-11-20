"""Signal handlers for account-related operations."""

import structlog
from django.db.models.signals import post_save
from django.dispatch import receiver

from accounts.models import RevelUser
from accounts.tasks import notify_admin_new_user_joined
from common.models import SiteSettings

logger = structlog.get_logger(__name__)


@receiver(post_save, sender=RevelUser)
def notify_admin_on_user_creation(
    sender: type[RevelUser], instance: RevelUser, created: bool, **kwargs: object
) -> None:
    """Send Pushover notification to admin when a new user joins.

    This is a standalone notification system that runs alongside the main notification
    system. It checks SiteSettings.notify_user_joined and dispatches a Celery task
    to send a Pushover notification if enabled.

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

    # Dispatch the Celery task asynchronously
    notify_admin_new_user_joined.delay(
        user_id=str(instance.id),
        user_email=instance.email,
        is_guest=instance.guest,
    )

    logger.info(
        "user_joined_notification_dispatched",
        user_id=str(instance.id),
        user_email=instance.email,
        is_guest=instance.guest,
    )
