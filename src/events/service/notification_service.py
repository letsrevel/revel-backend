"""Notification service for handling hierarchical user preferences and dispatching notifications."""

import logging
import typing as t
from enum import Enum

from django.db.models import Q, QuerySet

from accounts.models import RevelUser
from events.models import (
    Event,
    EventSeries,
    GeneralUserPreferences,
    Organization,
    UserEventPreferences,
    UserEventSeriesPreferences,
    UserOrganizationPreferences,
)

logger = logging.getLogger(__name__)


class NotificationType(Enum):
    """Types of notifications that can be sent."""

    EVENT_OPEN = "event_open"
    POTLUCK_UPDATE = "potluck_update"
    TICKET_CREATED = "ticket_created"
    TICKET_UPDATED = "ticket_updated"
    QUESTIONNAIRE_EVALUATION = "questionnaire_evaluation"
    QUESTIONNAIRE_SUBMITTED = "questionnaire_submitted"


def resolve_notification_preference(  # noqa: C901  # todo: refactor
    user: RevelUser,
    preference_name: str,
    *,
    event: Event | None = None,
    event_series: EventSeries | None = None,
    organization: Organization | None = None,
) -> bool:
    """Resolve a notification preference using the hierarchy.

    Hierarchy: UserEventPreferences -> UserEventSeriesPreferences ->
               UserOrganizationPreferences -> GeneralUserPreferences

    Args:
        user: The user whose preferences to resolve
        preference_name: Name of the preference field to resolve
        event: Optional event context
        event_series: Optional event series context
        organization: Optional organization context

    Returns:
        The resolved preference value
    """
    # Try UserEventPreferences first
    if event:
        try:
            event_prefs = UserEventPreferences.objects.get(user=user, event=event)
            value = getattr(event_prefs, preference_name, None)
            if value is not None:
                return t.cast(bool, value)
        except UserEventPreferences.DoesNotExist:
            pass

    # Try UserEventSeriesPreferences
    if event_series or (event and event.event_series):
        series = event_series or event.event_series  # type: ignore[union-attr]
        try:
            series_prefs = UserEventSeriesPreferences.objects.get(user=user, event_series=series)
            value = getattr(series_prefs, preference_name, None)
            if value is not None:
                return t.cast(bool, value)
        except UserEventSeriesPreferences.DoesNotExist:
            pass

    # Try UserOrganizationPreferences
    if organization or (event and event.organization):
        org = organization or event.organization  # type: ignore[union-attr]
        try:
            org_prefs = UserOrganizationPreferences.objects.get(user=user, organization=org)
            value = getattr(org_prefs, preference_name, None)
            if value is not None:
                return t.cast(bool, value)
        except UserOrganizationPreferences.DoesNotExist:
            pass

    # Fall back to GeneralUserPreferences
    try:
        general_prefs = GeneralUserPreferences.objects.get(user=user)
        value = getattr(general_prefs, preference_name, None)
        if value is not None:
            return t.cast(bool, value)
    except GeneralUserPreferences.DoesNotExist:
        pass

    # Default values based on preference type
    preference_defaults = {
        "silence_all_notifications": False,
        "is_subscribed": False,
        "notify_on_new_events": False,
        "notify_on_potluck_updates": False,
        "event_reminders": True,
    }

    return preference_defaults.get(preference_name, False)


def get_eligible_users_for_event_notification(event: Event, notification_type: NotificationType) -> QuerySet[RevelUser]:
    """Get users eligible to receive notifications for an event.

    Args:
        event: The event for which to send notifications
        notification_type: The type of notification being sent

    Returns:
        QuerySet of eligible users
    """
    # Start with all users who could potentially be notified
    # This includes members of the organization and users with preferences
    potential_users_q = Q()

    # Organization members
    if event.organization:
        potential_users_q |= Q(organization_memberships__organization=event.organization)

    # Users with event-specific preferences
    potential_users_q |= Q(usereventpreferences_preferences__event=event)

    # Users with series-specific preferences
    if event.event_series:
        potential_users_q |= Q(usereventseriespreferences_preferences__event_series=event.event_series)

    # Users with organization-specific preferences
    potential_users_q |= Q(userorganizationpreferences_preferences__organization=event.organization)

    # Get unique users
    potential_users = RevelUser.objects.filter(potential_users_q).distinct()

    # Filter based on resolved preferences
    eligible_users = []

    for user in potential_users:
        # Check if user has silenced all notifications
        if resolve_notification_preference(user, "silence_all_notifications", event=event):
            continue

        # Check if user is subscribed (at any level)
        if not resolve_notification_preference(user, "is_subscribed", event=event):
            continue

        # Check specific notification preferences
        if notification_type == NotificationType.EVENT_OPEN:
            if resolve_notification_preference(user, "notify_on_new_events", event=event):
                eligible_users.append(user.id)
        elif notification_type == NotificationType.POTLUCK_UPDATE:
            if resolve_notification_preference(user, "notify_on_potluck_updates", event=event):
                eligible_users.append(user.id)

    return RevelUser.objects.filter(id__in=eligible_users)


def get_organization_staff_and_owners(organization: Organization) -> QuerySet[RevelUser]:
    """Get all staff members and owners of an organization.

    Args:
        organization: The organization

    Returns:
        QuerySet of staff and owner users
    """
    return RevelUser.objects.filter(
        Q(owned_organizations=organization) | Q(organization_staff_memberships__organization=organization)
    ).distinct()


def should_notify_user_for_questionnaire(
    user: RevelUser, organization: Organization, notification_type: NotificationType
) -> bool:
    """Check if a user should be notified about questionnaire events.

    Args:
        user: The user to check
        organization: The organization context
        notification_type: Type of questionnaire notification

    Returns:
        True if user should be notified
    """
    # Check basic notification preferences
    if resolve_notification_preference(user, "silence_all_notifications", organization=organization):
        return False

    # For questionnaire notifications, we typically want to notify:
    # - Organization owners and staff (for submissions needing review)
    # - Users who submitted questionnaires (for evaluation results)

    return True  # More specific logic can be added based on requirements


def log_notification_attempt(
    user: RevelUser,
    notification_type: NotificationType,
    event: Event | None = None,
    success: bool = True,
    error_message: str | None = None,
) -> None:
    """Log notification attempts for debugging and audit purposes.

    Args:
        user: User who was targeted for notification
        notification_type: Type of notification
        event: Associated event (if any)
        success: Whether the notification was sent successfully
        error_message: Error message if notification failed
    """
    level = logging.INFO if success else logging.ERROR
    message = f"Notification {notification_type.value} {'sent to' if success else 'failed for'} user {user.email}"

    if event:
        message += f" for event {event.name} (ID: {event.id})"

    if error_message:
        message += f" - Error: {error_message}"

    logger.log(level, message)
