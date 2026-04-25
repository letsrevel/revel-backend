"""Tests for notification signal handlers."""

import typing as t
from decimal import Decimal
from unittest.mock import patch

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
        assert len(prefs.notification_type_settings) == 8

    def test_creates_preferences_for_guest_user_with_restrictions(
        self,
        django_user_model: type[RevelUser],
    ) -> None:
        """Test that guest users get restricted notification types.

        Guest users get 15 notification types disabled (only event participation
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

        # But have 15 notification types disabled
        assert len(prefs.notification_type_settings) == 15

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
