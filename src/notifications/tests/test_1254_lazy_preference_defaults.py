"""Lazy-created preference rows get the canonical defaults, not bare model defaults (#1254)."""

import pytest
from django.test.client import Client
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Organization
from notifications.enums import DeliveryChannel, NotificationType
from notifications.models import NotificationPreference
from notifications.schema import UpdateNotificationPreferenceSchema
from notifications.service import announcement_mute
from notifications.service.eligibility import is_user_eligible_for_notification
from notifications.service.unsubscribe import (
    confirm_unsubscribe,
    generate_unsubscribe_token,
    one_click_unsubscribe,
)

pytestmark = pytest.mark.django_db

EXPECTED_CHANNELS = [DeliveryChannel.IN_APP, DeliveryChannel.EMAIL]


def _drop_prefs(user: RevelUser) -> RevelUser:
    """Delete the signal-created row and return a fresh user instance (no cached relation)."""
    NotificationPreference.objects.filter(user=user).delete()
    return RevelUser.objects.get(pk=user.pk)


def _assert_regular_defaults(user: RevelUser) -> None:
    prefs = NotificationPreference.objects.get(user=user)
    assert prefs.enabled_channels == EXPECTED_CHANNELS


def _assert_guest_defaults(user: RevelUser) -> None:
    prefs = NotificationPreference.objects.get(user=user)
    assert prefs.enabled_channels == EXPECTED_CHANNELS
    assert prefs.notification_type_settings[NotificationType.ORG_ANNOUNCEMENT] == {"enabled": False}


def _client(user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")  # type: ignore[attr-defined]


class TestAnnouncementMute:
    def test_regular(self, regular_user: RevelUser, organization: Organization) -> None:
        user = _drop_prefs(regular_user)
        announcement_mute.mute_organization(user, organization)
        _assert_regular_defaults(user)

    def test_guest(self, guest_user: RevelUser, organization: Organization) -> None:
        user = _drop_prefs(guest_user)
        announcement_mute.mute_organization(user, organization)
        _assert_guest_defaults(user)


class TestUnsubscribe:
    def test_confirm_regular(self, regular_user: RevelUser) -> None:
        token = generate_unsubscribe_token(regular_user)
        _drop_prefs(regular_user)
        confirm_unsubscribe(token, UpdateNotificationPreferenceSchema.model_validate({}))
        _assert_regular_defaults(regular_user)

    def test_confirm_guest(self, guest_user: RevelUser) -> None:
        token = generate_unsubscribe_token(guest_user)
        _drop_prefs(guest_user)
        confirm_unsubscribe(token, UpdateNotificationPreferenceSchema.model_validate({}))
        _assert_guest_defaults(guest_user)

    def test_one_click_guest(self, guest_user: RevelUser) -> None:
        token = generate_unsubscribe_token(guest_user, notification_type=NotificationType.EVENT_OPEN)
        _drop_prefs(guest_user)
        one_click_unsubscribe(token)
        _assert_guest_defaults(guest_user)


class TestPreferenceController:
    def test_get_regular(self, regular_user: RevelUser) -> None:
        _drop_prefs(regular_user)
        assert _client(regular_user).get("/api/notification-preferences").status_code == 200
        _assert_regular_defaults(regular_user)

    def test_get_guest(self, guest_user: RevelUser) -> None:
        _drop_prefs(guest_user)
        assert _client(guest_user).get("/api/notification-preferences").status_code == 200
        _assert_guest_defaults(guest_user)

    def test_enable_channel_guest(self, guest_user: RevelUser) -> None:
        _drop_prefs(guest_user)
        response = _client(guest_user).post("/api/notification-preferences/enable-channel/email")
        assert response.status_code == 200
        _assert_guest_defaults(guest_user)


class TestEligibility:
    def test_missing_row_recreated_with_defaults(self, guest_user: RevelUser) -> None:
        user = _drop_prefs(guest_user)
        is_user_eligible_for_notification(user, NotificationType.ORG_ANNOUNCEMENT)
        _assert_guest_defaults(user)
