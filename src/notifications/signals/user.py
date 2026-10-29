"""Signal handlers for user-related notifications."""

import typing as t

import structlog
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

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


@receiver(pre_save, sender=RevelUser)
def capture_guest_flag_before_save(sender: type[RevelUser], instance: RevelUser, **kwargs: t.Any) -> None:
    """Remember a True guest flag in the DB when the instance is being saved as a non-guest."""
    if instance._state.adding or instance.guest:
        return
    update_fields = kwargs.get("update_fields")
    if update_fields is not None and "guest" not in update_fields:
        return  # e.g. the last_login save on every login: the guest flag can't change
    was_guest = RevelUser.objects.filter(pk=instance.pk, guest=True).exists()
    if was_guest:
        instance._was_guest = True  # type: ignore[attr-defined]


@receiver(post_save, sender=RevelUser)
def reset_preferences_on_guest_conversion(
    sender: type[RevelUser], instance: RevelUser, created: bool, **kwargs: object
) -> None:
    """Give a guest the regular notification defaults when it converts to a full account (#1248).

    Covers every conversion path (password reset, OIDC, Django admin).
    """
    if created or not instance.__dict__.pop("_was_guest", False):
        return
    reset_converted_guest_preferences(instance)
    logger.info("guest_notification_preferences_reset", user_id=str(instance.id))
