"""Notification digest service for batching notifications."""

import typing as t
from datetime import datetime, timedelta

import structlog
from django.db.models import QuerySet
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import gettext as _

from accounts.models import RevelUser
from notifications.enums import DeliveryChannel, DeliveryStatus, NotificationType
from notifications.models import Notification, NotificationPreference

logger = structlog.get_logger(__name__)


class DigestNotification(t.TypedDict):
    """Structure for a single notification row in a digest."""

    title: str
    body: str
    notification_type: str
    created_at: datetime
    context: dict[str, t.Any]
    url: str


class DigestGroup(t.TypedDict):
    """A group of digest notifications sharing the same type."""

    label: str
    notification_type: str
    notifications: list[DigestNotification]


class NotificationDigest:
    """Service for building and sending notification digests."""

    def __init__(self, user: RevelUser, notifications: QuerySet[Notification]):
        """Initialize digest.

        Args:
            user: User to send digest to
            notifications: QuerySet of notifications to include
        """
        self.user = user
        self.notifications = notifications

    def build_digest_content(self) -> tuple[str, str, str]:
        """Build digest email content.

        Returns:
            Tuple of (subject, text_body, html_body)
        """
        # Count totals
        total_count = self.notifications.count()

        # Build subject
        subject = _("%(count)d new notification%(plural)s") % {
            "count": total_count,
            "plural": "s" if total_count != 1 else "",
        }

        # Build bodies
        from common.models import SiteSettings
        from notifications.service.unsubscribe import generate_unsubscribe_token

        site_settings = SiteSettings.get_solo()
        frontend_base_url = site_settings.frontend_base_url

        # Group notifications by type (needs the base URL to build per-item CTAs)
        groups = self._build_notification_groups(frontend_base_url)

        # Generate unsubscribe link
        unsubscribe_token = generate_unsubscribe_token(self.user)
        unsubscribe_link = f"{frontend_base_url}/unsubscribe?token={unsubscribe_token}"

        context = {
            "user": self.user,
            "total_count": total_count,
            "notification_groups": groups,
            "digest_date": timezone.now(),
            "frontend_url": frontend_base_url,
            "frontend_base_url": frontend_base_url,
            "unsubscribe_link": unsubscribe_link,
        }

        text_body = render_to_string("notifications/emails/digest.txt", context)

        html_body = render_to_string("notifications/emails/digest.html", context)

        return subject, text_body, html_body

    def _build_notification_groups(self, frontend_base_url: str) -> list[DigestGroup]:
        """Group notifications by type, preserving chronological order within a group.

        Each group carries a human-readable label (the notification type's display
        name) and each row carries a CTA link derived from its context, so the
        template can render rich, actionable digest content.

        Args:
            frontend_base_url: Base URL used to build per-item CTAs and fallbacks.

        Returns:
            List of DigestGroup, ordered by first appearance of each type.
        """
        groups: dict[str, DigestGroup] = {}

        for notif in self.notifications.select_related("user").order_by("created_at"):
            notif_type = notif.notification_type

            if notif_type not in groups:
                groups[notif_type] = {
                    "label": _humanize_notification_type(notif_type),
                    "notification_type": notif_type,
                    "notifications": [],
                }

            label = groups[notif_type]["label"]
            # Resolve the best available title here (not in the template) so a row
            # always has a heading even when title is unrendered or the context
            # carries no event name.
            display_title = notif.title or notif.context.get("event_name") or label

            groups[notif_type]["notifications"].append(
                {
                    "title": display_title,
                    "body": notif.body or "",
                    "notification_type": notif_type,
                    "created_at": notif.created_at,
                    "context": notif.context,
                    "url": _build_notification_url(notif.context, frontend_base_url),
                }
            )

        return list(groups.values())

    def send_digest_email(self) -> bool:
        """Send digest email to user.

        Returns:
            True if digest was sent successfully
        """
        from common.tasks import send_email

        subject, text_body, html_body = self.build_digest_content()

        send_email.delay(to=self.user.email, subject=subject, body=text_body, html_body=html_body)

        logger.info(
            "digest_email_sent",
            user_id=str(self.user.id),
            notification_count=self.notifications.count(),
        )

        return True


def _humanize_notification_type(notification_type: str) -> str:
    """Return a human-readable label for a notification type.

    Falls back to a title-cased version of the raw value for any type that is no
    longer part of the enum (e.g. a deprecated type still present on old rows).

    Args:
        notification_type: Raw notification type value.

    Returns:
        Display label, e.g. "Event Reminder".
    """
    try:
        return str(NotificationType(notification_type).label)
    except ValueError:
        return notification_type.replace("_", " ").title()


def _build_notification_url(context: dict[str, t.Any], frontend_base_url: str) -> str:
    """Derive a CTA link for a digest row from its notification context.

    Prefers the most specific link available (event, then a generic action URL),
    falling back to the user's notifications page.

    Args:
        context: The notification's stored context.
        frontend_base_url: Base URL used for the fallback link.

    Returns:
        Absolute URL for the digest row's call to action.
    """
    url = context.get("event_url") or context.get("frontend_url") or context.get("action_url")
    if isinstance(url, str) and url:
        return url
    return f"{frontend_base_url}/notifications"


def get_pending_notifications_for_digest(user: RevelUser, since: datetime) -> QuerySet[Notification]:
    """Get notifications pending digest delivery.

    Only returns notifications that:
    - Were created since `since` timestamp
    - Have no successful email delivery yet
    - User hasn't marked as read

    Args:
        user: User to get notifications for
        since: Start of lookback period

    Returns:
        QuerySet of pending notifications
    """
    return (
        Notification.objects.filter(user=user, created_at__gte=since, read_at__isnull=True)
        .exclude(
            # Exclude if email was already sent
            deliveries__channel=DeliveryChannel.EMAIL,
            deliveries__status=DeliveryStatus.SENT,
        )
        .order_by("created_at")
    )


def should_send_digest_now(user: RevelUser) -> bool:
    """Check if it's time to send digest to user based on their preferences.

    For daily/weekly digests, checks if current time matches user's
    preferred send time (within 30 minute window).

    Args:
        user: User to check

    Returns:
        True if digest should be sent now
    """
    prefs = user.notification_preferences

    if prefs.digest_frequency == NotificationPreference.DigestFrequency.IMMEDIATE:
        return False  # No digest for immediate mode

    now = timezone.localtime(timezone.now())
    current_time = now.time()

    # Check if current time is close to user's preferred time (within 30 min window)
    preferred_time = prefs.digest_send_time
    time_diff = abs((current_time.hour * 60 + current_time.minute) - (preferred_time.hour * 60 + preferred_time.minute))

    if time_diff > 30:
        return False  # Not within send window

    # For weekly, also check day of week
    if prefs.digest_frequency == NotificationPreference.DigestFrequency.WEEKLY:
        # Send on Mondays
        if now.weekday() != 0:
            return False

    return True


def get_digest_lookback_period(frequency: str) -> timedelta:
    """Get the lookback period for digest based on frequency.

    Args:
        frequency: Digest frequency (hourly, daily, weekly)

    Returns:
        Timedelta for lookback period

    Raises:
        ValueError: If frequency is invalid
    """
    if frequency == NotificationPreference.DigestFrequency.HOURLY:
        return timedelta(hours=1)
    elif frequency == NotificationPreference.DigestFrequency.DAILY:
        return timedelta(days=1)
    elif frequency == NotificationPreference.DigestFrequency.WEEKLY:
        return timedelta(weeks=1)
    else:
        raise ValueError(f"Invalid digest frequency: {frequency}")
