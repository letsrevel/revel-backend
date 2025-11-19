"""Telegram notification channel implementation."""

import traceback

import structlog
from django.conf import settings
from django.utils import timezone, translation

from common.fields import render_markdown
from notifications.enums import DeliveryChannel, DeliveryStatus
from notifications.models import Notification, NotificationDelivery
from notifications.service.channels.base import NotificationChannel
from notifications.service.templates.registry import get_template
from notifications.utils import sanitize_for_telegram
from telegram.notification_keyboards import get_notification_keyboard
from telegram.tasks import send_message_task

logger = structlog.get_logger(__name__)


def update_delivery_status(delivery_id: str, status: str, error_message: str | None = None) -> None:
    """Callback function to update delivery status after Telegram sends message.

    This function is called by the telegram task after it completes (success or failure).

    Args:
        delivery_id: UUID of NotificationDelivery to update
        status: New status ("SENT" or "FAILED")
        error_message: Optional error message if failed
    """
    try:
        delivery = NotificationDelivery.objects.get(pk=delivery_id)

        delivery.status = DeliveryStatus(status)
        if status == DeliveryStatus.SENT:
            delivery.delivered_at = timezone.now()
        elif error_message:
            delivery.error_message = error_message

        delivery.save(update_fields=["status", "delivered_at", "error_message", "updated_at"])

        logger.info(
            "telegram_delivery_status_updated",
            delivery_id=delivery_id,
            status=status,
            error=error_message,
        )
    except NotificationDelivery.DoesNotExist:
        logger.error("delivery_not_found_for_callback", delivery_id=delivery_id)
    except Exception as e:
        logger.exception("delivery_status_update_failed", delivery_id=delivery_id, error=str(e), exc_info=True)


class TelegramChannel(NotificationChannel):
    """Telegram notification channel."""

    def get_channel_name(self) -> str:
        """Return channel name."""
        return DeliveryChannel.TELEGRAM

    def can_deliver(self, notification: Notification) -> bool:
        """Check if telegram message can be sent to user.

        Args:
            notification: The notification to check

        Returns:
            True if telegram message can be delivered
        """
        prefs = notification.user.notification_preferences

        # Check if telegram channel is enabled
        if not prefs.is_channel_enabled(DeliveryChannel.TELEGRAM):
            logger.debug(
                "telegram_channel_disabled",
                notification_id=str(notification.id),
                user_id=str(notification.user.id),
            )
            return False

        # Check if notification type is enabled
        if not prefs.is_notification_type_enabled(notification.notification_type):
            logger.debug(
                "notification_type_disabled",
                notification_id=str(notification.id),
                notification_type=notification.notification_type,
                user_id=str(notification.user.id),
            )
            return False

        # Check if user has telegram connected (get first active telegram account)
        tg_user = notification.user.telegram_users.filter(blocked_by_user=False, user_is_deactivated=False).first()

        if not tg_user:
            logger.debug(
                "telegram_not_connected",
                notification_id=str(notification.id),
                user_id=str(notification.user.id),
            )
            return False

        if not tg_user.telegram_id:
            logger.debug(
                "telegram_id_missing",
                notification_id=str(notification.id),
                user_id=str(notification.user.id),
            )
            return False

        return True

    def deliver(self, notification: Notification, delivery: NotificationDelivery) -> bool:
        """Send telegram notification with callback for status updates.

        Args:
            notification: The notification to deliver
            delivery: The delivery record to update

        Returns:
            True if delivery was dispatched successfully
        """
        delivery.attempted_at = timezone.now()
        delivery.retry_count += 1

        try:
            # Get first active telegram account
            tg_user = notification.user.telegram_users.filter(blocked_by_user=False, user_is_deactivated=False).first()

            if not tg_user:
                raise ValueError("No active telegram account found for user")

            # Format message for Telegram
            message = self._format_telegram_message(notification)

            # Build keyboard if applicable
            keyboard = get_notification_keyboard(notification)
            keyboard_dict = keyboard.model_dump(mode="json") if keyboard else None

            # Prepare callback data for status update
            callback_data = {
                "module": "notifications.service.channels.telegram",
                "function": "update_delivery_status",
                "kwargs": {
                    "delivery_id": str(delivery.id),
                },
            }

            # Get QR data (ticket ID for QR code) if applicable
            qr_data = self._get_qr_data(notification)

            # Send via telegram task with callback
            result = send_message_task.delay(
                tg_user.telegram_id,
                message=message,
                reply_markup=keyboard_dict,
                callback_data=callback_data,
                qr_data=qr_data,
            )

            # Mark as PENDING (will be updated by callback)
            delivery.metadata["telegram_task_id"] = result.id
            delivery.save(
                update_fields=[
                    "metadata",
                ]
            )

            logger.info(
                "telegram_notification_dispatched",
                notification_id=str(notification.id),
                notification_type=notification.notification_type,
                user_id=str(notification.user.id),
                task_id=result.id,
                callback_data=callback_data,
            )

            return True

        except Exception as e:
            delivery.status = DeliveryStatus.FAILED
            delivery.error_message = traceback.format_exc()
            delivery.save(update_fields=["status", "error_message", "retry_count", "attempted_at", "updated_at"])

            logger.exception(
                "telegram_notification_dispatch_failed",
                notification_id=str(notification.id),
                notification_type=notification.notification_type,
                user_id=str(notification.user.id),
                error=str(e),
            )

            return False

    def _get_qr_data(self, notification: Notification) -> str | None:
        """Get QR data for ticket notifications (ticket ID for QR code generation).

        Args:
            notification: The notification to check for QR code attachment

        Returns:
            Ticket ID string for QR code generation, or None if no QR code needed
        """
        from notifications.enums import NotificationType

        # Send QR code for ticket-related notifications
        ticket_notification_types = {
            NotificationType.TICKET_CREATED,
            NotificationType.TICKET_UPDATED,
        }

        if notification.notification_type in ticket_notification_types:
            # Only send QR code to ticket holder, not to staff/owners
            ticket_holder_name = notification.context.get("ticket_holder_name")
            if ticket_holder_name:
                # This is a notification to staff/owners, don't send QR code
                return None

            ticket_id = notification.context.get("ticket_id")
            if ticket_id:
                return str(ticket_id)

        return None

    def _format_telegram_message(self, notification: Notification) -> str:
        """Format notification for Telegram using HTML.

        The Telegram bot is configured to use HTML parse mode (see telegram/bot.py).
        We render a Telegram-specific template (markdown), convert it to HTML via
        MarkdownField's render_markdown(), and then sanitize for Telegram's limited
        HTML support.

        Telegram supports: <b>, <strong>, <i>, <em>, <u>, <ins>, <s>, <strike>, <del>,
        <code>, <pre>, <a href="">

        Args:
            notification: The notification to format

        Returns:
            Formatted HTML message for Telegram
        """
        # Get user's language
        user_language = getattr(notification.user, "language", settings.LANGUAGE_CODE)

        try:
            # Get template for this notification type
            template = get_template(notification.notification_type)

            # Render telegram-specific body in user's language
            with translation.override(user_language):
                telegram_body_md = template.get_telegram_body(notification)

            # Convert markdown to HTML
            html_body = render_markdown(telegram_body_md)

            # Sanitize for Telegram (remove unsupported tags)
            telegram_html = sanitize_for_telegram(html_body)

            # Add title in bold
            return f"<b>{notification.title}</b>\n\n{telegram_html}"

        except Exception as e:
            # Fallback to simple formatting if template fails
            logger.warning(
                "telegram_template_render_failed",
                notification_id=str(notification.id),
                notification_type=notification.notification_type,
                error=str(e),
            )
            # Use notification.body_html as fallback (already rendered markdown)
            # body_html is a property added dynamically by MarkdownField
            body_html = getattr(notification, "body_html", notification.body or "")
            fallback_html = sanitize_for_telegram(str(body_html))
            return f"<b>{notification.title}</b>\n\n{fallback_html}"
