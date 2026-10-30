"""Signal handlers for user-related notifications."""

import structlog
from django.db.models.signals import post_save
from django.dispatch import receiver

from accounts.dispatch import guest_converted
from accounts.models import RevelUser
from notifications.service.preferences import create_default_preferences, reset_converted_guest_preferences

logger = structlog.get_logger(__name__)


@receiver(post_save, sender=RevelUser)
def create_notification_preferences(
    sender: type[RevelUser], instance: RevelUser, created: bool, **kwargs: object
) -> None:
    """Create notification preferences when a new user is created.

    Args:
        sender: The model class (RevelUser)
        instance: The actual user instance being saved
        created: True if this is a new user
        **kwargs: Additional keyword arguments
    """
    if not created:
        return

    prefs, was_created = create_default_preferences(instance)

    if was_created:
        logger.info(
            "notification_preferences_created",
            user_id=str(instance.id),
            is_guest=instance.guest,
            enabled_channels=prefs.enabled_channels,
            restricted_types=len(prefs.notification_type_settings) if instance.guest else 0,
        )
    else:
        logger.debug(
            "notification_preferences_already_exist",
            user_id=str(instance.id),
        )


@receiver(guest_converted)
def reset_preferences_on_guest_converted(sender: type[RevelUser], user: RevelUser, **kwargs: object) -> None:
    """Give a converted guest the regular notification defaults (#1248).

    Not robust on purpose: a failure propagates to ``convert_guest`` and rolls the conversion back.
    """
    if reset_converted_guest_preferences(user):
        logger.info("guest_notification_preferences_reset", user_id=str(user.id))
