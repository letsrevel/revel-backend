"""Opt-out posture (#1030): mandatory types, digest opt-out, unsubscribe and resubscribe."""

from unittest.mock import MagicMock, patch

import pytest
from django.conf import settings
from django.test.client import Client
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.jwt import create_token
from accounts.models import RevelUser
from accounts.schema import UnsubscribeJWTPayloadSchema
from notifications.enums import MANDATORY_TYPES, DeliveryChannel, NotificationType
from notifications.models import (
    EmailSuppression,
    Notification,
    NotificationPreference,
    get_default_notification_type_settings,
)
from notifications.schema import UpdateNotificationPreferenceSchema
from notifications.service.channels.email import EmailChannel
from notifications.service.dispatcher import determine_delivery_channels
from notifications.service.email_policy import may_email
from notifications.service.unsubscribe import confirm_unsubscribe
from notifications.tasks import send_notification_digests

pytestmark = pytest.mark.django_db

EMAIL = DeliveryChannel.EMAIL
IN_APP = DeliveryChannel.IN_APP
ALL_TYPES = sorted(NotificationType)
OPT_OUT_SCENARIOS = ["silenced", "email_off", "type_disabled"]


def _apply_scenario(prefs: NotificationPreference, scenario: str, notification_type: str) -> None:
    if scenario == "silenced":
        prefs.silence_all_notifications = True
    elif scenario == "email_off":
        prefs.disable_channel(EMAIL)
    else:
        prefs.notification_type_settings[notification_type] = {"enabled": False}
    prefs.save()


def _unsubscribe_token(user: RevelUser) -> str:
    payload = UnsubscribeJWTPayloadSchema(
        user_id=user.id, email=user.email, exp=timezone.now() + settings.UNSUBSCRIBE_TOKEN_LIFETIME
    )
    return create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)


def _notify(user: RevelUser, notification_type: str) -> Notification:
    return Notification.objects.create(
        user=user, notification_type=notification_type, context={"k": "v"}, title="t", body="b"
    )


class TestMayEmailMatrix:
    """Every type x every opt-out: only mandatory types still email."""

    @pytest.mark.parametrize("scenario", OPT_OUT_SCENARIOS)
    @pytest.mark.parametrize("notification_type", ALL_TYPES)
    def test_may_email(self, regular_user: RevelUser, notification_type: str, scenario: str) -> None:
        _apply_scenario(regular_user.notification_preferences, scenario, notification_type)
        assert may_email(regular_user, notification_type) is (notification_type in MANDATORY_TYPES)

    @pytest.mark.parametrize("scenario", OPT_OUT_SCENARIOS)
    @pytest.mark.parametrize("notification_type", ALL_TYPES)
    def test_email_channel_can_deliver(self, regular_user: RevelUser, notification_type: str, scenario: str) -> None:
        _apply_scenario(regular_user.notification_preferences, scenario, notification_type)
        notification = _notify(regular_user, notification_type)
        assert EmailChannel().can_deliver(notification) is (notification_type in MANDATORY_TYPES)


class TestDetermineDeliveryChannels:
    @pytest.mark.parametrize("notification_type", sorted(MANDATORY_TYPES))
    def test_silenced_user_still_gets_mandatory_email_and_in_app(
        self, regular_user: RevelUser, notification_type: str
    ) -> None:
        _apply_scenario(regular_user.notification_preferences, "silenced", notification_type)
        channels = determine_delivery_channels(regular_user, notification_type)
        assert EMAIL in channels and IN_APP in channels

    @pytest.mark.parametrize("notification_type", sorted(MANDATORY_TYPES))
    def test_mandatory_types_skip_digest(self, regular_user: RevelUser, notification_type: str) -> None:
        prefs = regular_user.notification_preferences
        prefs.digest_frequency = NotificationPreference.DigestFrequency.DAILY
        prefs.silence_all_notifications = True
        prefs.save()
        assert EMAIL in determine_delivery_channels(regular_user, notification_type)

    @pytest.mark.parametrize("scenario", ["silenced", "type_disabled"])
    def test_digest_user_opted_out_gets_nothing(self, regular_user: RevelUser, scenario: str) -> None:
        prefs = regular_user.notification_preferences
        prefs.digest_frequency = NotificationPreference.DigestFrequency.DAILY
        _apply_scenario(prefs, scenario, NotificationType.EVENT_UPDATED)
        assert determine_delivery_channels(regular_user, NotificationType.EVENT_UPDATED) == []

    def test_digest_user_without_in_app_gets_nothing_now(self, regular_user: RevelUser) -> None:
        prefs = regular_user.notification_preferences
        prefs.digest_frequency = NotificationPreference.DigestFrequency.DAILY
        prefs.enabled_channels = [EMAIL]
        prefs.save()
        assert determine_delivery_channels(regular_user, NotificationType.EVENT_UPDATED) == []

    def test_digest_user_gets_in_app_only(self, regular_user: RevelUser) -> None:
        prefs = regular_user.notification_preferences
        prefs.digest_frequency = NotificationPreference.DigestFrequency.DAILY
        prefs.save()
        assert determine_delivery_channels(regular_user, NotificationType.EVENT_UPDATED) == [IN_APP]


@patch("notifications.service.digest.should_send_digest_now", return_value=True)
@patch("common.tasks.send_email.delay")
class TestDigestSweep:
    """The digest sweep honours silence, the email switch, per-type disables and suppression."""

    @pytest.fixture
    def digest_user(self, regular_user: RevelUser) -> RevelUser:
        prefs = regular_user.notification_preferences
        prefs.digest_frequency = NotificationPreference.DigestFrequency.DAILY
        prefs.save()
        _notify(regular_user, NotificationType.EVENT_UPDATED)
        _notify(regular_user, NotificationType.EVENT_OPEN)
        return regular_user

    def test_sends_digest_when_opted_in(self, send: MagicMock, _now: MagicMock, digest_user: RevelUser) -> None:
        assert send_notification_digests()["digests_sent"] == 1
        send.assert_called_once()

    def test_silenced_user_gets_no_digest(self, send: MagicMock, _now: MagicMock, digest_user: RevelUser) -> None:
        """Reproduces #1030: a silenced in-app-only user on a DAILY digest still got the email."""
        prefs = digest_user.notification_preferences
        prefs.silence_all_notifications = True
        prefs.enabled_channels = [IN_APP]
        prefs.save()
        assert send_notification_digests()["digests_sent"] == 0
        send.assert_not_called()

    def test_email_disabled_user_gets_no_digest(self, send: MagicMock, _now: MagicMock, digest_user: RevelUser) -> None:
        prefs = digest_user.notification_preferences
        prefs.disable_channel(EMAIL)
        prefs.save()
        assert send_notification_digests()["digests_sent"] == 0
        send.assert_not_called()

    def test_disabled_type_left_out_of_digest(self, send: MagicMock, _now: MagicMock, digest_user: RevelUser) -> None:
        prefs = digest_user.notification_preferences
        prefs.notification_type_settings[NotificationType.EVENT_OPEN] = {"enabled": False}
        prefs.save()
        with patch("notifications.service.digest.NotificationDigest") as digest_cls:
            digest_cls.return_value.send_digest_email.return_value = True
            send_notification_digests()
        (_, pending), _ = digest_cls.call_args
        assert {n.notification_type for n in pending} == {NotificationType.EVENT_UPDATED}

    def test_only_disabled_types_pending_sends_nothing(
        self, send: MagicMock, _now: MagicMock, digest_user: RevelUser
    ) -> None:
        prefs = digest_user.notification_preferences
        for notification_type in (NotificationType.EVENT_OPEN, NotificationType.EVENT_UPDATED):
            prefs.notification_type_settings[notification_type] = {"enabled": False}
        prefs.save()
        assert send_notification_digests()["digests_sent"] == 0
        send.assert_not_called()

    def test_suppressed_address_gets_no_digest(self, send: MagicMock, _now: MagicMock, digest_user: RevelUser) -> None:
        EmailSuppression.objects.create(
            email=digest_user.email,
            reason=EmailSuppression.Reason.HARD_BOUNCE,
            source=EmailSuppression.Source.PROVIDER,
        )
        assert send_notification_digests()["digests_sent"] == 0
        send.assert_not_called()

    def test_invitation_opt_out_does_not_block_digest(
        self, send: MagicMock, _now: MagicMock, digest_user: RevelUser
    ) -> None:
        EmailSuppression.objects.create(
            email=digest_user.email,
            reason=EmailSuppression.Reason.INVITATION_OPT_OUT,
            source=EmailSuppression.Source.RECIPIENT,
        )
        assert send_notification_digests()["digests_sent"] == 1


class TestUnsubscribeLeavesPerTypeSettingsAlone:
    def test_silence_submit_does_not_pin_per_type_channels(self, regular_user: RevelUser) -> None:
        before = regular_user.notification_preferences.notification_type_settings
        prefs = confirm_unsubscribe(
            _unsubscribe_token(regular_user),
            UpdateNotificationPreferenceSchema(silence_all_notifications=True, enabled_channels=["in_app"]),
        )
        prefs.refresh_from_db()
        assert prefs.notification_type_settings == before
        assert prefs.enabled_channels == [IN_APP]
        assert prefs.silence_all_notifications is True

    def test_email_ticked_keeps_org_contact_and_potluck_defaults(self, regular_user: RevelUser) -> None:
        prefs = confirm_unsubscribe(
            _unsubscribe_token(regular_user),
            UpdateNotificationPreferenceSchema(enabled_channels=["in_app", "email"]),
        )
        defaults = get_default_notification_type_settings()
        for notification_type in (NotificationType.ORG_CONTACT_MESSAGE_RECEIVED, NotificationType.POTLUCK_ITEM_CREATED):
            assert (
                prefs.get_channels_for_notification_type(notification_type) == defaults[notification_type]["channels"]
            )

    def test_unsubscribe_then_enable_email_resumes_email(self, regular_user: RevelUser) -> None:
        confirm_unsubscribe(
            _unsubscribe_token(regular_user),
            UpdateNotificationPreferenceSchema(enabled_channels=["in_app"]),
        )
        regular_user.refresh_from_db()
        assert may_email(regular_user, NotificationType.EVENT_UPDATED) is False

        refresh = RefreshToken.for_user(regular_user)
        client = Client(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")  # type: ignore[attr-defined]
        response = client.post("/api/notification-preferences/enable-channel/email")
        assert response.status_code == 200

        regular_user = RevelUser.objects.get(pk=regular_user.pk)
        assert may_email(regular_user, NotificationType.EVENT_UPDATED) is True


class TestEnableChannelEndpoint:
    def test_restores_default_per_type_override(self, regular_user: RevelUser) -> None:
        prefs = regular_user.notification_preferences
        prefs.enabled_channels = [IN_APP, EMAIL, DeliveryChannel.TELEGRAM]
        prefs.disable_channel(DeliveryChannel.TELEGRAM)
        prefs.save()

        refresh = RefreshToken.for_user(regular_user)
        client = Client(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")  # type: ignore[attr-defined]
        response = client.post("/api/notification-preferences/enable-channel/telegram")
        assert response.status_code == 200

        prefs.refresh_from_db()
        assert DeliveryChannel.TELEGRAM in prefs.enabled_channels
        org_contact = prefs.notification_type_settings[NotificationType.ORG_CONTACT_MESSAGE_RECEIVED]
        assert DeliveryChannel.TELEGRAM in org_contact["channels"]
