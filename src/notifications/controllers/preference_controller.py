"""API controller for notification preference management."""

from typing import Literal

from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from common.throttling import UserDefaultThrottle, WriteThrottle
from notifications.models import NotificationPreference
from notifications.schema import NotificationPreferenceSchema, UpdateNotificationPreferenceSchema

ChannelType = Literal["in_app", "email", "telegram"]


@api_controller(
    "/api/notification-preferences",
    tags=["Notification Preferences"],
    auth=I18nJWTAuth(),
    throttle=UserDefaultThrottle(),
)
class NotificationPreferenceController(UserAwareController):
    """API endpoints for notification preferences."""

    @route.get("", response=NotificationPreferenceSchema)
    def get_preferences(self) -> NotificationPreference:
        """Get current user's notification preferences."""
        prefs, _ = NotificationPreference.objects.get_or_create(user=self.user())
        return prefs

    @route.patch("", response=NotificationPreferenceSchema, throttle=WriteThrottle())
    def update_preferences(self, payload: UpdateNotificationPreferenceSchema) -> NotificationPreference:
        """Update notification preferences."""
        prefs, _ = NotificationPreference.objects.get_or_create(user=self.user())

        update_data = payload.model_dump(exclude_unset=True)

        if not update_data:
            return prefs

        for field, value in update_data.items():
            setattr(prefs, field, value)

        prefs.save(update_fields=list(update_data.keys()) + ["updated_at"])

        return prefs

    @route.post(
        "/enable-channel/{channel}",
        response=NotificationPreferenceSchema,
        throttle=WriteThrottle(),
    )
    def enable_channel(self, channel: ChannelType) -> NotificationPreference:
        """Enable a notification channel."""
        prefs, _ = NotificationPreference.objects.get_or_create(user=self.user())

        if channel not in prefs.enabled_channels:
            prefs.enabled_channels.append(channel)
            prefs.save(update_fields=["enabled_channels", "updated_at"])

        return prefs

    @route.post(
        "/disable-channel/{channel}",
        response=NotificationPreferenceSchema,
        throttle=WriteThrottle(),
    )
    def disable_channel(self, channel: ChannelType) -> NotificationPreference:
        """Disable a notification channel."""
        prefs, _ = NotificationPreference.objects.get_or_create(user=self.user())

        if channel in prefs.enabled_channels:
            prefs.enabled_channels.remove(channel)
            prefs.save(update_fields=["enabled_channels", "updated_at"])

        return prefs
