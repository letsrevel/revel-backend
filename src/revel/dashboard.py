"""Dashboard callback for Django Unfold admin interface."""

import typing as t
from datetime import date, timedelta

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


def _get_user_growth_data(days: int = 30) -> dict[str, t.Any]:
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


def _get_task_health(days: int = 7) -> dict[str, t.Any]:
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
    recent_failures = [
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


def _get_notification_health(days: int = 7) -> dict[str, t.Any]:
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
    recent_failures = [
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

    # Event analytics
    event_analytics = _get_event_analytics()

    # Task health
    task_health = _get_task_health(days=7)

    # Notification health
    notification_health = _get_notification_health(days=7)

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
    ]

    # Update context with dashboard data
    context.update(
        {
            "dashboard": {
                "quick_actions": quick_actions,
                "quick_stats": {
                    "total_users": total_users,
                    "connected_telegram": connected_telegram,
                    "total_organizations": total_organizations,
                    "total_events": total_events,
                },
                "user_growth": user_growth,
                "event_analytics": event_analytics,
                "task_health": task_health,
                "notification_health": notification_health,
            }
        }
    )

    return context
