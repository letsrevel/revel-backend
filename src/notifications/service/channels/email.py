"""Email notification channel implementation."""

import base64
from smtplib import SMTPException

import structlog
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

from common.models import EmailLog, SiteSettings
from common.tasks import to_safe_email_address
from notifications.enums import DeliveryChannel, DeliveryStatus
from notifications.models import Notification, NotificationDelivery
from notifications.service.channels.base import NotificationChannel

logger = structlog.get_logger(__name__)


class EmailChannel(NotificationChannel):
    """Email notification channel."""

    def get_channel_name(self) -> str:
        """Return channel name."""
        return DeliveryChannel.EMAIL

    def can_deliver(self, notification: Notification) -> bool:
        """Check if email can be sent to user.

        Args:
            notification: The notification to check

        Returns:
            True if email can be delivered
        """
        prefs = notification.user.notification_preferences

        # Check if email channel is enabled
        if not prefs.is_channel_enabled(DeliveryChannel.EMAIL):
            logger.debug(
                "email_channel_disabled",
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

        # Check if user has valid email
        if not notification.user.email:
            logger.warning(
                "user_missing_email",
                notification_id=str(notification.id),
                user_id=str(notification.user.id),
            )
            return False

        return True

    def deliver(self, notification: Notification, delivery: NotificationDelivery) -> bool:
        """Send email notification.

        Args:
            notification: The notification to deliver
            delivery: The delivery record to update

        Returns:
            True if delivery succeeded
        """
        from django.utils import translation

        delivery.attempted_at = timezone.now()
        delivery.retry_count += 1

        try:
            # Get email template
            from notifications.service.templates.registry import get_template

            template = get_template(notification.notification_type)

            # Get user's language
            user_language = getattr(notification.user, "language", settings.LANGUAGE_CODE)

            # Render email content in user's language
            with translation.override(user_language):
                subject = template.get_email_subject(notification)
                text_body = template.get_email_text_body(notification)
                html_body = template.get_email_html_body(notification)
                attachments = template.get_email_attachments(notification)

            # Get safe recipient email
            site_settings = SiteSettings.get_solo()
            recipient = to_safe_email_address(notification.user.email, site_settings=site_settings)

            # Build email
            email_msg = EmailMultiAlternatives(
                subject=subject,
                body=text_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[recipient],
            )

            if html_body:
                email_msg.attach_alternative(html_body, "text/html")

            # Attach files
            for filename, attachment_data in attachments.items():
                email_msg.attach(
                    filename,
                    base64.b64decode(attachment_data["content_base64"]),
                    attachment_data["mimetype"],
                )

            # Send
            email_msg.send(fail_silently=False)

            # Create EmailLog
            email_log = EmailLog(to=recipient, subject=subject)
            email_log.set_body(body=text_body)
            if html_body:
                email_log.set_html(html_body=html_body)
            email_log.save()

            # Update delivery record
            delivery.status = DeliveryStatus.SENT
            delivery.delivered_at = timezone.now()
            delivery.metadata["email_log_id"] = str(email_log.id)
            delivery.save(
                update_fields=[
                    "status",
                    "delivered_at",
                    "metadata",
                    "retry_count",
                    "attempted_at",
                    "updated_at",
                ]
            )

            logger.info(
                "email_notification_sent",
                notification_id=str(notification.id),
                notification_type=notification.notification_type,
                user_id=str(notification.user.id),
                email_log_id=str(email_log.id),
            )

            return True

        except Exception as e:
            delivery.status = DeliveryStatus.FAILED
            delivery.error_message = str(e)
            delivery.save(update_fields=["status", "error_message", "retry_count", "attempted_at", "updated_at"])

            logger.error(
                "email_notification_failed",
                notification_id=str(notification.id),
                notification_type=notification.notification_type,
                user_id=str(notification.user.id),
                error=str(e),
                retry_count=delivery.retry_count,
            )

            return False

    def should_retry(self, error: Exception) -> bool:
        """Determine if email delivery should be retried.

        Args:
            error: The exception that was raised

        Returns:
            True if delivery should be retried
        """
        retryable = (SMTPException, OSError, TimeoutError, ConnectionError)
        return isinstance(error, retryable)
