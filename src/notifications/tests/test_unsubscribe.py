"""Tests for notification unsubscribe functionality."""

from datetime import time
from unittest.mock import MagicMock, patch

import pytest
from django.conf import settings
from django.utils import timezone
from ninja.errors import HttpError

from accounts.jwt import blacklist as blacklist_token
from accounts.jwt import create_token
from accounts.models import RevelUser
from accounts.schema import UnsubscribeJWTPayloadSchema
from notifications.enums import NotificationType
from notifications.models import NotificationPreference
from notifications.schema import UpdateNotificationPreferenceSchema
from notifications.service.unsubscribe import confirm_unsubscribe, generate_unsubscribe_token

pytestmark = pytest.mark.django_db


# ============================================================================
# Token Generation Tests
# ============================================================================


def test_generate_unsubscribe_token_contains_user_data(user: RevelUser) -> None:
    """Test that generated token contains correct user information."""
    import jwt

    token = generate_unsubscribe_token(user)

    # Decode without verification to check payload
    payload = jwt.decode(token, options={"verify_signature": False})

    assert payload["user_id"] == str(user.id)
    assert payload["email"] == user.email
    assert payload["type"] == "unsubscribe"
    assert "exp" in payload
    assert "jti" in payload


# ============================================================================
# Confirm Unsubscribe - Success Cases
# ============================================================================


def test_confirm_unsubscribe_with_silence_all_enabled(user: RevelUser) -> None:
    """Test unsubscribe successfully enables silence_all_notifications."""
    # Arrange
    payload = UnsubscribeJWTPayloadSchema(
        user_id=user.id, email=user.email, exp=timezone.now() + settings.UNSUBSCRIBE_TOKEN_LIFETIME
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

    preferences = UpdateNotificationPreferenceSchema(silence_all_notifications=True)  # type: ignore[call-arg]

    # Act
    result = confirm_unsubscribe(token, preferences)

    # Assert
    assert result.silence_all_notifications is True
    assert result.user_id == user.id

    # Verify database state
    result.refresh_from_db()
    assert result.silence_all_notifications is True


def test_confirm_unsubscribe_with_disabled_channels(user: RevelUser) -> None:
    """Test unsubscribe successfully disables email channel."""
    # Arrange
    payload = UnsubscribeJWTPayloadSchema(
        user_id=user.id, email=user.email, exp=timezone.now() + settings.UNSUBSCRIBE_TOKEN_LIFETIME
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

    # User wants to disable email but keep in-app and telegram
    preferences = UpdateNotificationPreferenceSchema(enabled_channels=["in_app", "telegram"])

    # Act
    result = confirm_unsubscribe(token, preferences)

    # Assert
    assert "email" not in result.enabled_channels
    assert "in_app" in result.enabled_channels
    assert "telegram" in result.enabled_channels


def test_confirm_unsubscribe_with_multiple_fields(user: RevelUser) -> None:
    """Test unsubscribe updates multiple preference fields at once."""
    # Arrange
    payload = UnsubscribeJWTPayloadSchema(
        user_id=user.id, email=user.email, exp=timezone.now() + settings.UNSUBSCRIBE_TOKEN_LIFETIME
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

    preferences = UpdateNotificationPreferenceSchema(
        silence_all_notifications=False,
        enabled_channels=["in_app"],
        event_reminders_enabled=False,
        digest_frequency="daily",
        digest_send_time=time(14, 30),
    )

    # Act
    result = confirm_unsubscribe(token, preferences)

    # Assert
    assert result.silence_all_notifications is False
    assert result.enabled_channels == ["in_app"]
    assert result.event_reminders_enabled is False
    assert result.digest_frequency == "daily"
    assert result.digest_send_time == time(14, 30)


def test_confirm_unsubscribe_with_empty_preferences_returns_unchanged(user: RevelUser) -> None:
    """Test unsubscribe with empty preferences returns existing preferences unchanged."""
    # Arrange - Get existing preferences (created by signal) and update them
    existing_prefs = NotificationPreference.objects.get(user=user)
    existing_prefs.silence_all_notifications = True
    existing_prefs.enabled_channels = ["in_app"]
    existing_prefs.save()

    payload = UnsubscribeJWTPayloadSchema(
        user_id=user.id, email=user.email, exp=timezone.now() + settings.UNSUBSCRIBE_TOKEN_LIFETIME
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

    # Empty preferences - no fields set
    preferences = UpdateNotificationPreferenceSchema()  # type: ignore[call-arg]

    # Act
    result = confirm_unsubscribe(token, preferences)

    # Assert - Nothing should have changed
    assert result.id == existing_prefs.id
    assert result.silence_all_notifications is True
    assert result.enabled_channels == ["in_app"]


def test_confirm_unsubscribe_creates_preferences_if_none_exist(user: RevelUser) -> None:
    """Test unsubscribe works with auto-created preferences from signal."""
    # Arrange - Preferences are auto-created by signal when user is created
    assert NotificationPreference.objects.filter(user=user).exists()

    payload = UnsubscribeJWTPayloadSchema(
        user_id=user.id, email=user.email, exp=timezone.now() + settings.UNSUBSCRIBE_TOKEN_LIFETIME
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

    preferences = UpdateNotificationPreferenceSchema(silence_all_notifications=True)  # type: ignore[call-arg]

    # Act
    result = confirm_unsubscribe(token, preferences)

    # Assert
    assert NotificationPreference.objects.filter(user=user).count() == 1
    assert result.silence_all_notifications is True
    assert result.user == user


def test_confirm_unsubscribe_updates_existing_preferences(user: RevelUser) -> None:
    """Test unsubscribe updates existing preferences instead of creating new ones."""
    # Arrange - Get existing preferences (created by signal) and update them
    existing_prefs = NotificationPreference.objects.get(user=user)
    existing_prefs.silence_all_notifications = False
    existing_prefs.enabled_channels = ["email", "in_app"]
    existing_prefs.save()

    payload = UnsubscribeJWTPayloadSchema(
        user_id=user.id, email=user.email, exp=timezone.now() + settings.UNSUBSCRIBE_TOKEN_LIFETIME
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

    preferences = UpdateNotificationPreferenceSchema(silence_all_notifications=True)  # type: ignore[call-arg]

    # Act
    result = confirm_unsubscribe(token, preferences)

    # Assert - Same object updated
    assert result.id == existing_prefs.id
    assert NotificationPreference.objects.filter(user=user).count() == 1
    assert result.silence_all_notifications is True


def test_confirm_unsubscribe_with_notification_type_settings(user: RevelUser) -> None:
    """Test unsubscribe can update notification_type_settings."""
    # Arrange
    payload = UnsubscribeJWTPayloadSchema(
        user_id=user.id, email=user.email, exp=timezone.now() + settings.UNSUBSCRIBE_TOKEN_LIFETIME
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

    from notifications.enums import NotificationType
    from notifications.schema import NotificationTypeSettings

    preferences = UpdateNotificationPreferenceSchema(  # type: ignore[call-arg]
        notification_type_settings={
            NotificationType.TICKET_CREATED: NotificationTypeSettings(enabled=False, channels=[]),
            NotificationType.EVENT_REMINDER: NotificationTypeSettings(enabled=True, channels=["email"]),
        }
    )

    # Act
    result = confirm_unsubscribe(token, preferences)

    # Assert
    assert NotificationType.TICKET_CREATED in result.notification_type_settings
    assert result.notification_type_settings[NotificationType.TICKET_CREATED]["enabled"] is False
    assert NotificationType.EVENT_REMINDER in result.notification_type_settings
    assert result.notification_type_settings[NotificationType.EVENT_REMINDER]["enabled"] is True


# ============================================================================
# Confirm Unsubscribe - Error Cases
# ============================================================================


def test_confirm_unsubscribe_with_expired_token_raises_error(user: RevelUser) -> None:
    """Test unsubscribe with expired token raises HttpError."""
    # Arrange - Create an already-expired token
    payload = UnsubscribeJWTPayloadSchema(
        user_id=user.id, email=user.email, exp=timezone.now() - settings.UNSUBSCRIBE_TOKEN_LIFETIME
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

    preferences = UpdateNotificationPreferenceSchema(silence_all_notifications=True)  # type: ignore[call-arg]

    # Act & Assert
    with pytest.raises(HttpError) as exc_info:
        confirm_unsubscribe(token, preferences)

    assert exc_info.value.status_code == 400
    assert "expired" in str(exc_info.value.message).lower()


def test_confirm_unsubscribe_with_invalid_token_raises_error(user: RevelUser) -> None:
    """Test unsubscribe with malformed token raises HttpError."""
    # Arrange
    invalid_token = "this.is.not.a.valid.jwt.token"
    preferences = UpdateNotificationPreferenceSchema(silence_all_notifications=True)  # type: ignore[call-arg]

    # Act & Assert
    with pytest.raises(HttpError) as exc_info:
        confirm_unsubscribe(invalid_token, preferences)

    assert exc_info.value.status_code == 400
    assert "invalid" in str(exc_info.value.message).lower()


def test_confirm_unsubscribe_with_blacklisted_token_raises_error(user: RevelUser) -> None:
    """Test unsubscribe with already-used (blacklisted) token raises HttpError."""
    # Arrange
    payload = UnsubscribeJWTPayloadSchema(
        user_id=user.id, email=user.email, exp=timezone.now() + settings.UNSUBSCRIBE_TOKEN_LIFETIME
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

    # Blacklist the token (simulate it being used already)
    blacklist_token(token)

    preferences = UpdateNotificationPreferenceSchema(silence_all_notifications=True)  # type: ignore[call-arg]

    # Act & Assert
    with pytest.raises(HttpError) as exc_info:
        confirm_unsubscribe(token, preferences)

    assert exc_info.value.status_code == 401
    assert "blacklist" in str(exc_info.value.message).lower()


def test_confirm_unsubscribe_with_nonexistent_user_raises_404(user: RevelUser) -> None:
    """Test unsubscribe with token for deleted user raises 404."""
    # Arrange
    from django.http import Http404

    user_id = user.id
    payload = UnsubscribeJWTPayloadSchema(
        user_id=user_id, email=user.email, exp=timezone.now() + settings.UNSUBSCRIBE_TOKEN_LIFETIME
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

    # Delete the user
    user.delete()

    preferences = UpdateNotificationPreferenceSchema(silence_all_notifications=True)  # type: ignore[call-arg]

    # Act & Assert - get_object_or_404 raises Http404, not HttpError
    with pytest.raises(Http404):
        confirm_unsubscribe(token, preferences)


def test_confirm_unsubscribe_with_duplicate_channels_raises_validation_error(user: RevelUser) -> None:
    """Test unsubscribe with duplicate channels in enabled_channels raises validation error."""
    # Arrange
    payload = UnsubscribeJWTPayloadSchema(
        user_id=user.id, email=user.email, exp=timezone.now() + settings.UNSUBSCRIBE_TOKEN_LIFETIME
    )
    create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

    # This should fail validation at the schema level
    with pytest.raises(ValueError, match="unique"):
        UpdateNotificationPreferenceSchema(enabled_channels=["email", "email", "in_app"])


# ============================================================================
# Integration Tests
# ============================================================================


@patch("notifications.service.unsubscribe.logger")
def test_confirm_unsubscribe_logs_correctly(mock_logger: MagicMock, user: RevelUser) -> None:
    """Test that confirm_unsubscribe logs the operation correctly."""
    # Arrange
    payload = UnsubscribeJWTPayloadSchema(
        user_id=user.id, email=user.email, exp=timezone.now() + settings.UNSUBSCRIBE_TOKEN_LIFETIME
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

    preferences = UpdateNotificationPreferenceSchema(silence_all_notifications=True)  # type: ignore[call-arg]

    # Act
    confirm_unsubscribe(token, preferences)

    # Assert - Check logging calls
    assert mock_logger.info.call_count >= 2
    # First call should be unsubscribe confirmed
    first_call = mock_logger.info.call_args_list[0]
    assert "unsubscribe_confirmed" in first_call[0]

    # Second call should be preferences updated
    second_call = mock_logger.info.call_args_list[1]
    assert "unsubscribe_preferences_updated" in second_call[0]


def test_confirm_unsubscribe_partial_update_preserves_other_fields(user: RevelUser) -> None:
    """Test that partial updates only change specified fields."""
    # Arrange - Get existing preferences (created by signal) and update them
    existing_prefs = NotificationPreference.objects.get(user=user)
    existing_prefs.silence_all_notifications = False
    existing_prefs.enabled_channels = ["email", "in_app", "telegram"]
    existing_prefs.event_reminders_enabled = True
    existing_prefs.digest_frequency = "immediate"
    existing_prefs.save()

    payload = UnsubscribeJWTPayloadSchema(
        user_id=user.id, email=user.email, exp=timezone.now() + settings.UNSUBSCRIBE_TOKEN_LIFETIME
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

    # Only update enabled_channels
    preferences = UpdateNotificationPreferenceSchema(enabled_channels=["in_app"])

    # Act
    result = confirm_unsubscribe(token, preferences)

    # Assert - Only enabled_channels changed
    assert result.enabled_channels == ["in_app"]
    # These should remain unchanged
    assert result.silence_all_notifications is False
    assert result.event_reminders_enabled is True
    assert result.digest_frequency == "immediate"


def test_confirm_unsubscribe_idempotent_with_same_values(user: RevelUser) -> None:
    """Test that applying the same preferences multiple times is idempotent."""
    # Arrange - Get existing preferences (created by signal) and update them
    existing_prefs = NotificationPreference.objects.get(user=user)
    existing_prefs.silence_all_notifications = False
    existing_prefs.enabled_channels = ["email"]
    existing_prefs.save()

    payload = UnsubscribeJWTPayloadSchema(
        user_id=user.id, email=user.email, exp=timezone.now() + settings.UNSUBSCRIBE_TOKEN_LIFETIME
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

    preferences = UpdateNotificationPreferenceSchema(silence_all_notifications=True)  # type: ignore[call-arg]

    # Act - Apply twice (second token needed since first is not blacklisted by this service)
    result1 = confirm_unsubscribe(token, preferences)

    # Generate new token for second attempt
    payload2 = UnsubscribeJWTPayloadSchema(
        user_id=user.id, email=user.email, exp=timezone.now() + settings.UNSUBSCRIBE_TOKEN_LIFETIME
    )
    token2 = create_token(payload2.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

    result2 = confirm_unsubscribe(token2, preferences)

    # Assert - Same result both times
    assert result1.silence_all_notifications == result2.silence_all_notifications is True
    assert result1.id == result2.id  # Same preference object


def test_confirm_unsubscribe_syncs_enabled_channels_to_all_notification_types(user: RevelUser) -> None:
    """Test that updating enabled_channels syncs to ALL notification_type_settings.

    This ensures that notification types without explicit settings don't slip through
    via default behavior (enabled=True, channels=all).
    """
    # Arrange - Get existing preferences and set some custom per-type settings
    existing_prefs = NotificationPreference.objects.get(user=user)
    existing_prefs.enabled_channels = ["email", "in_app", "telegram"]
    existing_prefs.notification_type_settings = {
        NotificationType.EVENT_REMINDER: {"enabled": True, "channels": ["email"]},
        NotificationType.TICKET_CREATED: {"enabled": False, "channels": ["in_app"]},
        # All other types are NOT explicitly set (would default to enabled=True, all channels)
    }
    existing_prefs.save()

    payload = UnsubscribeJWTPayloadSchema(
        user_id=user.id, email=user.email, exp=timezone.now() + settings.UNSUBSCRIBE_TOKEN_LIFETIME
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

    # User unsubscribes from email and telegram, only wants in_app
    preferences = UpdateNotificationPreferenceSchema(enabled_channels=["in_app"])

    # Act
    result = confirm_unsubscribe(token, preferences)

    # Assert - enabled_channels updated
    assert result.enabled_channels == ["in_app"]

    # Assert - ALL notification types now have explicit entries with new channels
    for notif_type in NotificationType:
        assert notif_type.value in result.notification_type_settings
        type_setting = result.notification_type_settings[notif_type.value]

        # Channels should be synced to ["in_app"] for ALL types
        assert type_setting["channels"] == ["in_app"]

        # Enabled status should be preserved for types that had explicit settings
        if notif_type == NotificationType.EVENT_REMINDER:
            assert type_setting["enabled"] is True  # Was True, should remain True
        elif notif_type == NotificationType.TICKET_CREATED:
            assert type_setting["enabled"] is False  # Was False, should remain False
        else:
            # All other types default to enabled=True
            assert type_setting["enabled"] is True


def test_confirm_unsubscribe_preserves_enabled_status_when_syncing_channels(user: RevelUser) -> None:
    """Test that syncing channels preserves the enabled status of each notification type."""
    # Arrange - User has disabled some notification types
    existing_prefs = NotificationPreference.objects.get(user=user)
    existing_prefs.enabled_channels = ["email", "in_app"]
    existing_prefs.notification_type_settings = {
        NotificationType.POTLUCK_ITEM_CREATED: {"enabled": False, "channels": ["in_app"]},
        NotificationType.POTLUCK_ITEM_CLAIMED: {"enabled": False, "channels": ["in_app"]},
        NotificationType.EVENT_REMINDER: {"enabled": True, "channels": ["email", "in_app"]},
    }
    existing_prefs.save()

    payload = UnsubscribeJWTPayloadSchema(
        user_id=user.id, email=user.email, exp=timezone.now() + settings.UNSUBSCRIBE_TOKEN_LIFETIME
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

    # User removes email from enabled_channels
    preferences = UpdateNotificationPreferenceSchema(enabled_channels=["in_app"])

    # Act
    result = confirm_unsubscribe(token, preferences)

    # Assert - Disabled types remain disabled
    assert result.notification_type_settings[NotificationType.POTLUCK_ITEM_CREATED]["enabled"] is False
    assert result.notification_type_settings[NotificationType.POTLUCK_ITEM_CLAIMED]["enabled"] is False

    # But their channels are updated
    assert result.notification_type_settings[NotificationType.POTLUCK_ITEM_CREATED]["channels"] == ["in_app"]
    assert result.notification_type_settings[NotificationType.POTLUCK_ITEM_CLAIMED]["channels"] == ["in_app"]

    # Enabled types remain enabled
    assert result.notification_type_settings[NotificationType.EVENT_REMINDER]["enabled"] is True
    assert result.notification_type_settings[NotificationType.EVENT_REMINDER]["channels"] == ["in_app"]
