"""Dashboard callback for Django Unfold admin interface."""

import typing as t
from datetime import date, datetime, timedelta

from dateutil.relativedelta import relativedelta
from django.contrib.auth.models import AnonymousUser
from django.db.models import Count
from django.http import HttpRequest
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Event, Organization
from notifications.enums import DeliveryStatus
from notifications.models import NotificationDelivery
from telegram.models import TelegramUser


class PronounDistribution(t.TypedDict):
    """Pie-chart data for the pronoun distribution card."""

    labels: list[str]
    data: list[int]
    total: int


class UserGrowthData(t.TypedDict):
    """Line-chart data for the user-growth card."""

    labels: list[str]
    data: list[int]


class RecentTaskFailure(t.TypedDict):
    """A single recent Celery task failure rendered in the task-health card."""

    task_name: str | None
    date_done: datetime | None
    task_id: str


class TaskHealth(t.TypedDict):
    """Celery task-health summary."""

    failed_count: int
    recent_failures: list[RecentTaskFailure]
    days: int


class RecentNotificationFailure(t.TypedDict):
    """A single recent notification-delivery failure rendered in the notification-health card."""

    notification_type: str
    channel: str
    created_at: datetime
    error_message: str


class NotificationHealth(t.TypedDict):
    """Notification-delivery health summary."""

    failed_count: int
    channel_breakdown: dict[str, int]
    recent_failures: list[RecentNotificationFailure]
    days: int


class MaintenanceBanner(t.TypedDict):
    """Active maintenance-banner data for the dashboard."""

    message: str
    severity: SiteSettings.BannerSeverity
    scheduled_at: datetime | None
    ends_at: datetime | None
    accent: str
    label: str
    icon: str


class TopOrganizationRow(t.TypedDict):
    """A single ranked row in the top-organizations-by-traction table."""

    rank: int
    name: str
    distinct_users: int
    change_url: str


class TopOrganizationsPayload(t.TypedDict):
    """Chart series and ranked table rows for the top-organizations-by-traction card."""

    labels: list[str]
    data: list[int]
    rows: list[TopOrganizationRow]


def _get_pronoun_distribution() -> PronounDistribution:
    """Calculate pronoun distribution among users.

    Returns:
        Dictionary with labels and data for pie chart
    """
    # Get pronoun counts, excluding empty strings
    pronoun_stats = (
        RevelUser.objects.exclude(pronouns="").values("pronouns").annotate(count=Count("id")).order_by("-count")
    )

    # Count users without pronouns
    no_pronouns_count = RevelUser.objects.filter(pronouns="").count()

    labels = [stat["pronouns"] for stat in pronoun_stats]
    data = [stat["count"] for stat in pronoun_stats]

    # Add "Not specified" category if there are users without pronouns
    if no_pronouns_count > 0:
        labels.append("Not specified")
        data.append(no_pronouns_count)

    return {
        "labels": labels,
        "data": data,
        "total": sum(data),
    }


def _get_user_growth_data(days: int = 30) -> UserGrowthData:
    """Calculate user growth statistics over the specified period.

    Args:
        days: Number of days to look back (default: 30)

    Returns:
        Dictionary with labels (week labels) and data (user counts per week)
    """
    end_date = timezone.now()
    start_date = end_date - timedelta(days=days)

    # Query users created in the time period
    users = RevelUser.objects.filter(date_joined__gte=start_date, date_joined__lte=end_date).values_list(
        "date_joined", flat=True
    )

    # Group by week, storing both the date and count
    weekly_data: dict[date, int] = {}
    for date_joined in users:
        # Get the start of the week (Monday)
        week_start = date_joined - timedelta(days=date_joined.weekday())
        # Use date only (no time) as key
        week_key = week_start.date()
        weekly_data[week_key] = weekly_data.get(week_key, 0) + 1

    # Sort by actual date and format labels
    sorted_weeks = sorted(weekly_data.items(), key=lambda x: x[0])

    return {
        "labels": [week[0].strftime("%b %d") for week in sorted_weeks],
        "data": [week[1] for week in sorted_weeks],
    }


def _get_event_analytics() -> dict[str, t.Any]:
    """Calculate event analytics statistics.

    Returns:
        Dictionary with event statistics by RSVP/ticket requirement, visibility, and event type
    """
    total_events = Event.objects.count()

    # Avoid division by zero
    if total_events == 0:
        return {
            "total": 0,
            "by_ticket_requirement": {"rsvp_based": 0, "ticket_required": 0},
            "by_visibility": {},
            "by_event_type": {},
        }

    # RSVP vs Requires Ticket
    rsvp_count = Event.objects.filter(requires_ticket=False).count()
    ticket_count = Event.objects.filter(requires_ticket=True).count()

    # By Visibility
    visibility_stats = Event.objects.values("visibility").annotate(count=Count("id"))
    visibility_data = {stat["visibility"]: stat["count"] for stat in visibility_stats}

    # By Event Type
    event_type_stats = Event.objects.values("event_type").annotate(count=Count("id"))
    event_type_data = {stat["event_type"]: stat["count"] for stat in event_type_stats}

    return {
        "total": total_events,
        "by_ticket_requirement": {
            "rsvp_based": {"count": rsvp_count, "percentage": round((rsvp_count / total_events) * 100, 1)},
            "ticket_required": {"count": ticket_count, "percentage": round((ticket_count / total_events) * 100, 1)},
        },
        "by_visibility": {
            key: {"count": value, "percentage": round((value / total_events) * 100, 1)}
            for key, value in visibility_data.items()
        },
        "by_event_type": {
            key: {"count": value, "percentage": round((value / total_events) * 100, 1)}
            for key, value in event_type_data.items()
        },
    }


def _get_task_health(days: int = 7) -> TaskHealth:
    """Get Celery task health statistics.

    Args:
        days: Number of days to look back (default: 7)

    Returns:
        Dictionary with task failure statistics
    """
    from django_celery_results.models import TaskResult

    cutoff_date = timezone.now() - timedelta(days=days)

    # Failed tasks in the last N days
    failed_tasks = TaskResult.objects.filter(status="FAILURE", date_done__gte=cutoff_date).order_by("-date_done")

    failed_count = failed_tasks.count()
    recent_failures: list[RecentTaskFailure] = [
        {
            "task_name": task.task_name,
            "date_done": task.date_done,
            "task_id": task.task_id,
        }
        for task in failed_tasks[:10]  # Limit to 10 most recent
    ]

    return {
        "failed_count": failed_count,
        "recent_failures": recent_failures,
        "days": days,
    }


def _get_notification_health(days: int = 7) -> NotificationHealth:
    """Get notification delivery health statistics.

    Args:
        days: Number of days to look back (default: 7)

    Returns:
        Dictionary with notification delivery failure statistics
    """
    cutoff_date = timezone.now() - timedelta(days=days)

    # Failed deliveries in the last N days
    failed_deliveries = NotificationDelivery.objects.filter(
        status=DeliveryStatus.FAILED, created_at__gte=cutoff_date
    ).select_related("notification")

    failed_count = failed_deliveries.count()

    # Breakdown by channel
    channel_stats = failed_deliveries.values("channel").annotate(count=Count("id"))
    channel_breakdown = {stat["channel"]: stat["count"] for stat in channel_stats}

    # Recent failures
    recent_failures: list[RecentNotificationFailure] = [
        {
            "notification_type": delivery.notification.notification_type,
            "channel": delivery.channel,
            "created_at": delivery.created_at,
            "error_message": delivery.error_message[:100] if delivery.error_message else "",
        }
        for delivery in failed_deliveries.order_by("-created_at")[:10]  # Limit to 10 most recent
    ]

    return {
        "failed_count": failed_count,
        "channel_breakdown": channel_breakdown,
        "recent_failures": recent_failures,
        "days": days,
    }


def _get_top_organizations_by_traction(months: int = 12, limit: int = 10) -> TopOrganizationsPayload:
    """Get the top organizations by user traction over a trailing window.

    Traction is the number of distinct users an organization reached via event
    RSVPs (yes/maybe) or tickets (active/checked_in/pending) — see
    ``OrganizationQuerySet.top_by_traction`` for the exact metric.

    Args:
        months: Size of the trailing window in months (default: 12).
        limit: Maximum number of organizations to include (default: 10).

    Returns:
        Dictionary with ``labels`` and ``data`` for the bar chart and ``rows``
        (rank, name, distinct_users, change_url) for the ranked table.
    """
    since = timezone.now() - relativedelta(months=months)
    rows = Organization.objects.top_by_traction(since=since, limit=limit)

    table_rows: list[TopOrganizationRow] = [
        {
            "rank": rank,
            "name": row.name,
            "distinct_users": row.distinct_users,
            "change_url": reverse("admin:events_organization_change", args=[row.organization_id]),
        }
        for rank, row in enumerate(rows, start=1)
    ]

    return {
        "labels": [row.name for row in rows],
        "data": [row.distinct_users for row in rows],
        "rows": table_rows,
    }


_BANNER_SEVERITY_STYLES: dict[SiteSettings.BannerSeverity, dict[str, str]] = {
    SiteSettings.BannerSeverity.DEBUG: {
        "accent": "bg-gray-400",
        "label": "text-gray-600 dark:text-gray-400",
        "icon": "🔧",
    },
    SiteSettings.BannerSeverity.INFO: {
        "accent": "bg-blue-500",
        "label": "text-blue-600 dark:text-blue-400",
        "icon": "ℹ️",
    },
    SiteSettings.BannerSeverity.WARNING: {
        "accent": "bg-yellow-500",
        "label": "text-yellow-600 dark:text-yellow-400",
        "icon": "⚠️",
    },
    SiteSettings.BannerSeverity.ERROR: {
        "accent": "bg-red-500",
        "label": "text-red-600 dark:text-red-400",
        "icon": "🚨",
    },
    SiteSettings.BannerSeverity.CRITICAL: {
        "accent": "bg-red-600",
        "label": "text-red-600 dark:text-red-400",
        "icon": "🔴",
    },
}


def _get_maintenance_banner(site_settings: SiteSettings) -> MaintenanceBanner | None:
    """Return maintenance banner data if a banner is currently active.

    A banner is active when maintenance_message is non-empty and
    maintenance_ends_at is either null or in the future.

    Args:
        site_settings: The SiteSettings singleton instance.

    Returns:
        Dictionary with banner data for the template, or None.
    """
    if not site_settings.is_maintenance_banner_active:
        return None

    severity = SiteSettings.BannerSeverity(site_settings.maintenance_severity)
    styles = _BANNER_SEVERITY_STYLES.get(severity, _BANNER_SEVERITY_STYLES[SiteSettings.BannerSeverity.INFO])

    return {
        "message": site_settings.maintenance_message,
        "severity": severity,
        "scheduled_at": site_settings.maintenance_scheduled_at,
        "ends_at": site_settings.maintenance_ends_at,
        "accent": styles["accent"],
        "label": styles["label"],
        "icon": styles["icon"],
    }


def dashboard_callback(request: HttpRequest, context: dict[str, t.Any]) -> dict[str, t.Any]:
    """Prepare custom variables for the admin dashboard.

    This callback is called by Django Unfold to inject custom data into the
    admin index template. Only staff and superusers can access the dashboard.

    Args:
        request: The HTTP request object
        context: The existing template context

    Returns:
        Updated context dictionary with dashboard data
    """
    user = request.user

    # Only show dashboard to staff/superusers
    if isinstance(user, AnonymousUser) or not (user.is_staff or user.is_superuser):
        return context

    # Get site settings
    site_settings = SiteSettings.get_solo()

    # Quick stats
    total_users = RevelUser.objects.count()
    connected_telegram = TelegramUser.objects.filter(user__isnull=False).count()
    total_organizations = Organization.objects.count()
    total_events = Event.objects.count()

    # User growth data
    user_growth = _get_user_growth_data(days=30)

    # Pronoun distribution
    pronoun_distribution = _get_pronoun_distribution()

    # Event analytics
    event_analytics = _get_event_analytics()

    # Top organizations by user traction (last 12 months)
    top_organizations = _get_top_organizations_by_traction(months=12, limit=10)

    # Task health
    task_health = _get_task_health(days=7)

    # Notification health
    notification_health = _get_notification_health(days=7)

    # Active maintenance banner
    maintenance_banner = _get_maintenance_banner(site_settings)

    # Quick action links
    quick_actions = [
        {
            "title": "Go to Frontend",
            "url": site_settings.frontend_base_url,
            "icon": "🌐",
        },
        {
            "title": "Site Settings",
            "url": reverse("admin:common_sitesettings_change", args=[site_settings.pk]),
            "icon": "⚙️",
        },
        {
            "title": "Grafana",
            "url": "https://grafana.letsrevel.io",
            "icon": "📊",
        },
        {
            "title": "Send Announcement",
            "url": reverse("admin:notifications_notification_send_announcement"),
            "icon": "📢",
        },
    ]

    # Update context with dashboard data
    context.update(
        {
            "dashboard": {
                "maintenance_banner": maintenance_banner,
                "quick_actions": quick_actions,
                "quick_stats": {
                    "total_users": total_users,
                    "connected_telegram": connected_telegram,
                    "total_organizations": total_organizations,
                    "total_events": total_events,
                },
                "user_growth": user_growth,
                "pronoun_distribution": pronoun_distribution,
                "event_analytics": event_analytics,
                "top_organizations": top_organizations,
                "task_health": task_health,
                "notification_health": notification_health,
            }
        }
    )

    return context
