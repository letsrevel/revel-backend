"""Notification preference defaults for new users and guest -> account conversion (#1248)."""

import typing as t

from django.db import transaction

from accounts.models import RevelUser
from notifications.enums import DeliveryChannel, NotificationType
from notifications.models import NotificationPreference, get_default_notification_type_settings
from notifications.types import NotificationTypeSetting

# Both guest and regular users get in-app + email channels
# (Guest users can't login to see in-app, but we enable it anyway)
DEFAULT_ENABLED_CHANNELS = [DeliveryChannel.IN_APP, DeliveryChannel.EMAIL]


def get_guest_notification_type_settings() -> dict[NotificationType, NotificationTypeSetting]:
    """Get conservative notification type settings for guest users.

    Guest users only receive notifications strictly necessary for event participation.
    They do NOT receive: event discovery, org announcements, membership, potluck, etc.

    Returns:
        Dictionary with notification types disabled for guests
    """
    # Disable all notification types except those essential for event participation
    disabled_types = [
        NotificationType.EVENT_OPEN,  # Can't browse events
        NotificationType.POTLUCK_ITEM_CREATED,  # Typically not participating
        NotificationType.POTLUCK_ITEM_CREATED_AND_CLAIMED,
        NotificationType.POTLUCK_ITEM_UPDATED,
        NotificationType.POTLUCK_ITEM_CLAIMED,
        NotificationType.POTLUCK_ITEM_UNCLAIMED,
        NotificationType.QUESTIONNAIRE_SUBMITTED,  # For staff, not guests
        NotificationType.INVITATION_REQUEST_CREATED,  # For organizers, not guests
        NotificationType.MEMBERSHIP_REQUEST_CREATED,  # For organizers, not guests
        NotificationType.MEMBERSHIP_GRANTED,  # Can't be members
        NotificationType.MEMBERSHIP_PROMOTED,
        NotificationType.MEMBERSHIP_REMOVED,
        NotificationType.MEMBERSHIP_CARD_UPDATED,
        NotificationType.MEMBERSHIP_REQUEST_APPROVED,
        NotificationType.MEMBERSHIP_REQUEST_REJECTED,
        NotificationType.ORG_ANNOUNCEMENT,  # Not members
        NotificationType.ORG_CONTACT_MESSAGE_RECEIVED,  # For org admins, not guests
        NotificationType.STAFF_TICKET_CREATED,  # Staff alerts: guests can't be staff
        NotificationType.STAFF_TICKET_CANCELLED,
        NotificationType.STAFF_TICKET_REFUNDED,
        NotificationType.STAFF_PAYMENT_DISPUTED,
        NotificationType.STAFF_SERIES_PASS_PURCHASED,
        NotificationType.STAFF_SERIES_PASS_CANCELLED,
    ]

    return {
        notification_type: t.cast(NotificationTypeSetting, {"enabled": False}) for notification_type in disabled_types
    }


def create_default_preferences(user: RevelUser) -> tuple[NotificationPreference, bool]:
    """Get or create the user's preferences with guest or regular defaults.

    Args:
        user: The user to create preferences for.

    Returns:
        ``(preferences, created)``.
    """
    # Guest users get restricted notification types (only event participation essentials)
    # Regular users get the regular defaults
    notification_type_settings = (
        get_guest_notification_type_settings() if user.guest else get_default_notification_type_settings()
    )
    # get_or_create handles race conditions and duplicate signals
    return NotificationPreference.objects.get_or_create(
        user=user,
        defaults={
            "enabled_channels": DEFAULT_ENABLED_CHANNELS,
            "notification_type_settings": notification_type_settings,
        },
    )


def _is_guest_default(value: t.Any) -> bool:
    """True for a disabled override of a guest-default key, however it was written.

    Covers ``{"enabled": False}`` (guest creation), ``{"enabled": False, "channels": []}`` (written
    back by the preference API and ``confirm_unsubscribe``) and legacy pre-#1030 rows that still pin
    a ``channels`` list on a disabled entry. Channels are irrelevant while a type is disabled.
    """
    return isinstance(value, dict) and not value.get("enabled", True)


def reset_converted_guest_preferences(user: RevelUser) -> bool:
    """Give a just-converted guest the regular notification defaults.

    Any disabled guest-default entry (``{"enabled": False}`` or one that still lists channels) is
    replaced with the regular default for that key (or removed if there is none), and regular-default keys the guest
    row never had are added. Any other value is an explicit user choice and is kept. A missing
    preferences row is created with the regular defaults.

    The runtime rule (any disabled guest-default key) is broader than the
    ``reset_converted_guest_prefs`` backfill command because at conversion time the
    values are known to be guest-written; the backfill only sees rows of unknown history and so
    resets just the original keys.

    # ponytail: any disabled guest-default entry is reset, so a guest who deliberately turned one
    # of those types off (only possible via an unsubscribe link, guests can't log in) gets it back
    # on conversion. Upgrade path: record the origin of each override.

    Args:
        user: The user just converted from guest to full account (``user.guest`` is False).

    Returns:
        True if preferences were created or changed.
    """
    with transaction.atomic():
        prefs = NotificationPreference.objects.select_for_update().filter(user=user).first()
        if prefs is None:
            create_default_preferences(user)
            return True
        regular_defaults = get_default_notification_type_settings()
        current = dict(prefs.notification_type_settings) if isinstance(prefs.notification_type_settings, dict) else {}
        updated = dict(current)
        for key in get_guest_notification_type_settings():
            if _is_guest_default(updated.get(key)):
                if key in regular_defaults:
                    updated[key] = regular_defaults[key]
                else:
                    del updated[key]
        for key, regular_value in regular_defaults.items():
            updated.setdefault(key, regular_value)
        if updated == current:
            return False
        prefs.notification_type_settings = updated
        prefs.save(update_fields=["notification_type_settings", "updated_at"])
        return True
