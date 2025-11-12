"""Signals for the notification system.

This module defines signals used for event-driven notification dispatch.
"""

import typing as t

import structlog
from django.db.models.signals import post_save
from django.dispatch import Signal, receiver

from accounts.models import RevelUser
from notifications.enums import DeliveryChannel, NotificationType
from notifications.models import NotificationPreference

logger = structlog.get_logger(__name__)


# Signal for requesting notification dispatch
# Expected kwargs:
#   - notification_type: NotificationType enum value
#   - user: RevelUser instance
#   - context: dict matching the notification type's context schema
notification_requested = Signal()


def _get_guest_notification_type_settings() -> dict[str, t.Any]:
    """Get conservative notification type settings for guest users.

    Guest users only receive notifications strictly necessary for event participation.
    They do NOT receive: event discovery, org announcements, membership, potluck, etc.

    Returns:
        Dictionary with notification types disabled for guests
    """
    # Disable all notification types except those essential for event participation
    disabled_types = [
        NotificationType.EVENT_OPEN,  # Can't browse events
        NotificationType.EVENT_CREATED,  # Can't follow organizations
        NotificationType.POTLUCK_ITEM_CREATED,  # Typically not participating
        NotificationType.POTLUCK_ITEM_UPDATED,
        NotificationType.POTLUCK_ITEM_CLAIMED,
        NotificationType.POTLUCK_ITEM_UNCLAIMED,
        NotificationType.QUESTIONNAIRE_SUBMITTED,  # For staff, not guests
        NotificationType.INVITATION_CLAIMED,  # For organizers, not guests
        NotificationType.MEMBERSHIP_GRANTED,  # Can't be members
        NotificationType.MEMBERSHIP_PROMOTED,
        NotificationType.MEMBERSHIP_REMOVED,
        NotificationType.MEMBERSHIP_REQUEST_APPROVED,
        NotificationType.MEMBERSHIP_REQUEST_REJECTED,
        NotificationType.ORG_ANNOUNCEMENT,  # Not members
        NotificationType.MALWARE_DETECTED,  # System/admin only
    ]

    return {notification_type: {"enabled": False} for notification_type in disabled_types}


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

    # Both guest and regular users get in-app + email channels
    # (Guest users can't login to see in-app, but we enable it anyway)
    enabled_channels = [DeliveryChannel.IN_APP, DeliveryChannel.EMAIL]

    # Guest users get restricted notification types (only event participation essentials)
    # Regular users get all notification types enabled (default empty dict)
    notification_type_settings = _get_guest_notification_type_settings() if instance.guest else {}

    # Use get_or_create to handle race conditions and duplicate signals
    prefs, was_created = NotificationPreference.objects.get_or_create(
        user=instance,
        defaults={
            "enabled_channels": enabled_channels,
            "notification_type_settings": notification_type_settings,
        },
    )

    if was_created:
        logger.info(
            "notification_preferences_created",
            user_id=str(instance.id),
            is_guest=instance.guest,
            enabled_channels=enabled_channels,
            restricted_types=len(notification_type_settings) if instance.guest else 0,
        )
    else:
        logger.debug(
            "notification_preferences_already_exist",
            user_id=str(instance.id),
        )
