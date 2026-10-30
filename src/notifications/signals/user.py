"""Signal handlers for user-related notifications."""

from uuid import UUID

import structlog
from django.db import transaction
from django.db.models.signals import post_init, post_save
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


@receiver(post_init, sender=RevelUser)
def record_loaded_guest_flag(sender: type[RevelUser], instance: RevelUser, **kwargs: object) -> None:
    """Remember the guest flag as loaded, so post_save can detect a guest -> account transition."""
    if "guest" in instance.__dict__:  # skip instances loaded with ``.only()``/``.defer()`` on guest
        instance.__dict__["_loaded_guest"] = instance.guest


def _reset_preferences(user_id: UUID) -> None:
    user = RevelUser.objects.filter(pk=user_id, guest=False).first()
    if user is not None and reset_converted_guest_preferences(user):
        logger.info("guest_notification_preferences_reset", user_id=str(user_id))


@receiver(post_save, sender=RevelUser)
def reset_preferences_on_guest_conversion(
    sender: type[RevelUser], instance: RevelUser, created: bool, **kwargs: object
) -> None:
    """Give a guest the regular notification defaults when it converts to a full account (#1248).

    Covers every conversion path (password reset, OIDC, Django admin). The reset runs on commit
    (robust: a failure is logged, never raised) so a preference problem cannot roll back or fail
    the conversion itself.
    """
    if created or kwargs.get("raw") or not instance.__dict__.get("_loaded_guest") or instance.guest:
        return
    instance.__dict__["_loaded_guest"] = False
    transaction.on_commit(lambda: _reset_preferences(instance.pk), robust=True)
