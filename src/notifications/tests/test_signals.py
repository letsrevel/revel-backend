"""Tests for notification signal handlers."""

import typing as t
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.db import transaction

from accounts.models import RevelUser
from events.models import Payment, Ticket, TicketTier
from notifications.enums import DeliveryChannel, NotificationType
from notifications.models import NotificationPreference
from telegram.models import TelegramUser
from telegram.signals import telegram_account_linked, telegram_account_unlinked

pytestmark = pytest.mark.django_db


class TestCreateNotificationPreferences:
    """Test automatic creation of notification preferences on user creation."""

    def test_creates_preferences_for_regular_user(
        self,
        django_user_model: type[RevelUser],
    ) -> None:
        """Test that notification preferences are created for regular users.

        Regular users should have both IN_APP and EMAIL channels enabled,
        with no notification type restrictions (empty notification_type_settings).
        """
        # Act - Creating user triggers signal
        user = django_user_model.objects.create_user(
            username="test@example.com",
            email="test@example.com",
            password="password",
            guest=False,
        )

        # Assert - Preferences created automatically
        assert hasattr(user, "notification_preferences")
        prefs = user.notification_preferences

        assert prefs is not None
        assert DeliveryChannel.IN_APP in prefs.enabled_channels
        assert DeliveryChannel.EMAIL in prefs.enabled_channels

        # Regular users have custom settings for certain notification types:
        # - 6 potluck notifications (IN_APP only, no email)
        # - 2 follow-admin notifications (IN_APP only, no email)
        # - 1 org contact message (IN_APP + TELEGRAM only, no email)
        assert len(prefs.notification_type_settings) == 9

    def test_creates_preferences_for_guest_user_with_restrictions(
        self,
        django_user_model: type[RevelUser],
    ) -> None:
        """Test that guest users get restricted notification types.

        Guest users get 16 notification types disabled (only event participation
        essentials like TICKET_CREATED, EVENT_REMINDER, etc. remain enabled).
        """
        # Act
        user = django_user_model.objects.create_user(
            username="guest@example.com",
            email="guest@example.com",
            password="password",
            guest=True,
        )

        # Assert
        prefs = user.notification_preferences

        # Guest users still get both channels
        assert DeliveryChannel.IN_APP in prefs.enabled_channels
        assert DeliveryChannel.EMAIL in prefs.enabled_channels

        # But have 16 notification types disabled
        assert len(prefs.notification_type_settings) == 16

        # Verify specific disabled types
        disabled_types = [
            NotificationType.EVENT_OPEN,
            NotificationType.POTLUCK_ITEM_CREATED,
            NotificationType.POTLUCK_ITEM_CREATED_AND_CLAIMED,
            NotificationType.POTLUCK_ITEM_UPDATED,
            NotificationType.POTLUCK_ITEM_CLAIMED,
            NotificationType.POTLUCK_ITEM_UNCLAIMED,
            NotificationType.QUESTIONNAIRE_SUBMITTED,
            NotificationType.INVITATION_REQUEST_CREATED,
            NotificationType.MEMBERSHIP_REQUEST_CREATED,
            NotificationType.MEMBERSHIP_GRANTED,
            NotificationType.MEMBERSHIP_PROMOTED,
            NotificationType.MEMBERSHIP_REMOVED,
            NotificationType.MEMBERSHIP_REQUEST_APPROVED,
            NotificationType.MEMBERSHIP_REQUEST_REJECTED,
            NotificationType.ORG_ANNOUNCEMENT,
        ]

        for notif_type in disabled_types:
            assert notif_type in prefs.notification_type_settings
            assert prefs.notification_type_settings[notif_type]["enabled"] is False

    def test_handles_duplicate_signal_with_get_or_create(
        self,
        django_user_model: type[RevelUser],
    ) -> None:
        """Test that duplicate signal calls don't cause integrity errors.

        The signal uses get_or_create to handle race conditions where
        the signal might be called multiple times.
        """
        # Create user (triggers signal)
        user = django_user_model.objects.create_user(
            username="duplicate@example.com",
            email="duplicate@example.com",
            password="password",
        )

        # Manually try to create again (simulating duplicate signal)
        prefs, created = NotificationPreference.objects.get_or_create(
            user=user,
            defaults={
                "enabled_channels": [DeliveryChannel.IN_APP],
            },
        )

        # Should not create duplicate
        assert created is False
        assert prefs.user == user

        # Should only have one preference
        assert NotificationPreference.objects.filter(user=user).count() == 1


class TestTelegramChannelSignals:
    """Test that linking/unlinking Telegram adds/removes the TELEGRAM delivery channel."""

    _next_telegram_id = 100000

    def _create_user_and_tg_user(
        self, django_user_model: type[RevelUser], username: str
    ) -> tuple[RevelUser, TelegramUser]:
        user = django_user_model.objects.create_user(
            username=username,
            email=f"{username}@example.com",
            password="password",
        )
        TestTelegramChannelSignals._next_telegram_id += 1
        tg_user = TelegramUser.objects.create(
            telegram_id=TestTelegramChannelSignals._next_telegram_id, telegram_username="tguser"
        )
        return user, tg_user

    def test_telegram_channel_enabled_on_link(
        self,
        django_user_model: type[RevelUser],
    ) -> None:
        """Linking a Telegram account adds TELEGRAM to enabled_channels."""
        user, tg_user = self._create_user_and_tg_user(django_user_model, "link_test")
        prefs = NotificationPreference.objects.get(user=user)
        assert DeliveryChannel.TELEGRAM not in prefs.enabled_channels

        telegram_account_linked.send(sender=TelegramUser, user=user, telegram_user=tg_user)

        prefs.refresh_from_db()
        assert DeliveryChannel.TELEGRAM in prefs.enabled_channels

    def test_telegram_channel_disabled_on_unlink(
        self,
        django_user_model: type[RevelUser],
    ) -> None:
        """Unlinking a Telegram account removes TELEGRAM from enabled_channels."""
        user, tg_user = self._create_user_and_tg_user(django_user_model, "unlink_test")
        prefs = NotificationPreference.objects.get(user=user)
        prefs.enabled_channels.append(DeliveryChannel.TELEGRAM)
        prefs.save(update_fields=["enabled_channels"])

        telegram_account_unlinked.send(sender=TelegramUser, user=user, telegram_user=tg_user)

        prefs.refresh_from_db()
        assert DeliveryChannel.TELEGRAM not in prefs.enabled_channels

    def test_enable_is_idempotent(
        self,
        django_user_model: type[RevelUser],
    ) -> None:
        """Sending the linked signal twice does not duplicate the channel."""
        user, tg_user = self._create_user_and_tg_user(django_user_model, "idempotent_enable")

        telegram_account_linked.send(sender=TelegramUser, user=user, telegram_user=tg_user)
        telegram_account_linked.send(sender=TelegramUser, user=user, telegram_user=tg_user)

        prefs = NotificationPreference.objects.get(user=user)
        assert prefs.enabled_channels.count(DeliveryChannel.TELEGRAM) == 1

    def test_disable_is_idempotent(
        self,
        django_user_model: type[RevelUser],
    ) -> None:
        """Sending the unlinked signal when TELEGRAM is not present is a no-op."""
        user, tg_user = self._create_user_and_tg_user(django_user_model, "idempotent_disable")
        prefs = NotificationPreference.objects.get(user=user)
        assert DeliveryChannel.TELEGRAM not in prefs.enabled_channels

        telegram_account_unlinked.send(sender=TelegramUser, user=user, telegram_user=tg_user)

        prefs.refresh_from_db()
        assert DeliveryChannel.TELEGRAM not in prefs.enabled_channels


@pytest.mark.django_db(transaction=True)
class TestPaymentRefundNotificationAmount:
    """Tests that refund notifications report payment.refund_amount, not payment.amount."""

    def test_refund_notification_reports_partial_refund_amount(
        self,
        public_event: t.Any,
        member_user: RevelUser,
    ) -> None:
        """Refund notification must report payment.refund_amount, not payment.amount."""
        tier = TicketTier.objects.create(
            event=public_event,
            name="Test Tier",
            price=Decimal("40.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.ONLINE,
        )
        ticket = Ticket.objects.create(
            guest_name=member_user.get_display_name(),
            event=public_event,
            user=member_user,
            tier=tier,
            status=Ticket.TicketStatus.ACTIVE,
        )
        payment = Payment.objects.create(
            ticket=ticket,
            user=member_user,
            amount=Decimal("40.00"),
            platform_fee=Decimal("0.00"),
            currency="EUR",
            status=Payment.PaymentStatus.SUCCEEDED,
            stripe_session_id=f"cs_test_{ticket.id}",
        )
        payment.refund_amount = Decimal("20.00")
        with patch("notifications.signals.payment.notification_requested.send") as send_mock:
            with transaction.atomic():
                payment.status = Payment.PaymentStatus.REFUNDED
                payment.save()
        contexts = [call.kwargs["context"] for call in send_mock.call_args_list]
        assert any("20.00" in c["refund_amount"] for c in contexts), contexts
        assert all("40.00" not in c["refund_amount"] for c in contexts), contexts


class TestEventCancelledNotificationReason:
    """Tests that EVENT_CANCELLED notifications carry the persisted cancellation reason."""

    def _build_eligible(self, recipient: RevelUser) -> t.Any:
        eligible = MagicMock()
        eligible.__iter__.return_value = iter([recipient])
        eligible.count.return_value = 1
        return eligible

    def test_reason_included_in_context_when_set(
        self,
        public_event: t.Any,
        user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """A non-empty cancellation_reason is surfaced in the notification context."""
        from notifications.signals import event as event_signals

        public_event.cancellation_reason = "Venue flooded"

        with (
            patch.object(
                event_signals, "get_eligible_users_for_event_notification", return_value=self._build_eligible(user)
            ),
            patch.object(event_signals, "_get_event_location_for_user", return_value=("Somewhere", None)),
            patch("notifications.signals.event.notification_requested.send") as send_mock,
            django_capture_on_commit_callbacks(execute=True),
        ):
            event_signals._handle_event_cancelled(type(public_event), public_event)

        contexts = [call.kwargs["context"] for call in send_mock.call_args_list]
        assert contexts, "expected at least one cancellation notification"
        assert all(c.get("cancellation_reason") == "Venue flooded" for c in contexts), contexts

    def test_reason_omitted_from_context_when_blank(
        self,
        public_event: t.Any,
        user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """An empty cancellation_reason leaves the key out of the context entirely."""
        from notifications.signals import event as event_signals

        public_event.cancellation_reason = ""

        with (
            patch.object(
                event_signals, "get_eligible_users_for_event_notification", return_value=self._build_eligible(user)
            ),
            patch.object(event_signals, "_get_event_location_for_user", return_value=("Somewhere", None)),
            patch("notifications.signals.event.notification_requested.send") as send_mock,
            django_capture_on_commit_callbacks(execute=True),
        ):
            event_signals._handle_event_cancelled(type(public_event), public_event)

        contexts = [call.kwargs["context"] for call in send_mock.call_args_list]
        assert contexts, "expected at least one cancellation notification"
        assert all("cancellation_reason" not in c for c in contexts), contexts
