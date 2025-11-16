"""Service layer for notification unsubscribe functionality."""

import structlog
from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils import timezone

from accounts.jwt import check_blacklist, create_token
from accounts.models import RevelUser
from accounts.schema import UnsubscribeJWTPayloadSchema
from accounts.service.account import token_to_payload
from notifications.models import NotificationPreference
from notifications.schema import UpdateNotificationPreferenceSchema

logger = structlog.get_logger(__name__)


def generate_unsubscribe_token(user: RevelUser) -> str:
    """Generate an unsubscribe token for a user.

    Args:
        user: The user to generate the token for

    Returns:
        The unsubscribe token
    """
    payload = UnsubscribeJWTPayloadSchema(
        user_id=user.id,
        email=user.email,
        exp=timezone.now() + settings.UNSUBSCRIBE_TOKEN_LIFETIME,
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)
    logger.debug("unsubscribe_token_generated", user_id=str(user.id))
    return token


def confirm_unsubscribe(token: str, preferences: UpdateNotificationPreferenceSchema) -> NotificationPreference:
    """Confirm and execute notification preference update via unsubscribe token.

    When enabled_channels is updated, this function also syncs the change to ALL
    notification_type_settings entries to ensure no notification types slip through
    via default behavior.

    Args:
        token: The unsubscribe token
        preferences: The notification preferences to update

    Returns:
        The updated notification preferences
    """
    from notifications.enums import NotificationType

    payload = token_to_payload(token, UnsubscribeJWTPayloadSchema)
    check_blacklist(payload.jti)
    user = get_object_or_404(RevelUser, id=payload.user_id)

    logger.info("unsubscribe_confirmed", user_id=str(user.id), email=user.email)

    # Get or create notification preferences
    prefs, _ = NotificationPreference.objects.get_or_create(user=user)

    # Update preferences using the same logic as the authenticated endpoint
    update_data = preferences.model_dump(exclude_unset=True)

    if not update_data:
        return prefs

    # If enabled_channels is being updated, sync to all notification_type_settings
    if "enabled_channels" in update_data:
        new_channels = update_data["enabled_channels"]
        existing_settings = prefs.notification_type_settings.copy()

        # Set ALL notification types to use the new enabled_channels
        for notif_type in NotificationType:
            # Preserve enabled status if it exists, otherwise default to True
            current_setting = existing_settings.get(notif_type.value, {})
            enabled = current_setting.get("enabled", True)

            existing_settings[notif_type.value] = {
                "enabled": enabled,
                "channels": new_channels,
            }

        # Update notification_type_settings in the update_data
        update_data["notification_type_settings"] = existing_settings

    for field, value in update_data.items():
        setattr(prefs, field, value)

    prefs.save(update_fields=list(update_data.keys()) + ["updated_at"])

    logger.info(
        "unsubscribe_preferences_updated",
        user_id=str(user.id),
        email=user.email,
        updated_fields=list(update_data.keys()),
    )

    return prefs
