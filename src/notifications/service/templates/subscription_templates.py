"""Templates for membership-subscription notifications.

Titles/subjects are built here; the in-app, email, and Telegram bodies render
from the on-disk templates under ``templates/notifications/*/subscription_*``.
"""

from django.utils.translation import gettext as _

from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.base import NotificationTemplate
from notifications.service.templates.registry import register_template


class SubscriptionRenewalSucceededTemplate(NotificationTemplate):
    """Template for SUBSCRIPTION_RENEWAL_SUCCEEDED notification."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        org_name = notification.context.get("organization_name", "")
        return _("Membership renewed: %(org)s") % {"org": org_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        org_name = notification.context.get("organization_name", "")
        return _("Your %(org)s membership was renewed") % {"org": org_name}


class SubscriptionPaymentFailedTemplate(NotificationTemplate):
    """Template for SUBSCRIPTION_PAYMENT_FAILED notification."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        org_name = notification.context.get("organization_name", "")
        return _("Payment failed: %(org)s membership") % {"org": org_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        org_name = notification.context.get("organization_name", "")
        return _("Action needed: payment failed for %(org)s") % {"org": org_name}


class SubscriptionExpiredTemplate(NotificationTemplate):
    """Template for SUBSCRIPTION_EXPIRED notification."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        org_name = notification.context.get("organization_name", "")
        return _("Membership expired: %(org)s") % {"org": org_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        org_name = notification.context.get("organization_name", "")
        return _("Your %(org)s membership has expired") % {"org": org_name}


class SubscriptionCancellationConfirmedTemplate(NotificationTemplate):
    """Template for SUBSCRIPTION_CANCELLATION_CONFIRMED notification."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        org_name = notification.context.get("organization_name", "")
        return _("Membership cancelled: %(org)s") % {"org": org_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        org_name = notification.context.get("organization_name", "")
        return _("Your %(org)s membership has been cancelled") % {"org": org_name}


class SubscriptionRenewalReminderTemplate(NotificationTemplate):
    """Template for SUBSCRIPTION_RENEWAL_REMINDER notification."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        org_name = notification.context.get("organization_name", "")
        return _("Membership renews soon: %(org)s") % {"org": org_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        org_name = notification.context.get("organization_name", "")
        return _("Your %(org)s membership renews soon") % {"org": org_name}


class SubscriptionPriceMigrationNoticeTemplate(NotificationTemplate):
    """Template for SUBSCRIPTION_PRICE_MIGRATION_NOTICE notification."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        org_name = notification.context.get("organization_name", "")
        return _("Price change: %(org)s membership") % {"org": org_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        org_name = notification.context.get("organization_name", "")
        return _("Upcoming price change for your %(org)s membership") % {"org": org_name}


# Register templates
register_template(NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED, SubscriptionRenewalSucceededTemplate())
register_template(NotificationType.SUBSCRIPTION_PAYMENT_FAILED, SubscriptionPaymentFailedTemplate())
register_template(NotificationType.SUBSCRIPTION_EXPIRED, SubscriptionExpiredTemplate())
register_template(NotificationType.SUBSCRIPTION_CANCELLATION_CONFIRMED, SubscriptionCancellationConfirmedTemplate())
register_template(NotificationType.SUBSCRIPTION_RENEWAL_REMINDER, SubscriptionRenewalReminderTemplate())
register_template(NotificationType.SUBSCRIPTION_PRICE_MIGRATION_NOTICE, SubscriptionPriceMigrationNoticeTemplate())
