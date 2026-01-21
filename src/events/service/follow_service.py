"""Service functions for following organizations and event series."""

import typing as t
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import Model, QuerySet
from ninja.errors import HttpError

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import EventSeries, Organization, OrganizationMember
from events.models.follow import EventSeriesFollow, OrganizationFollow
from notifications.enums import NotificationType
from notifications.service.eligibility import get_staff_for_notification
from notifications.signals import notification_requested

# Type variable for follow models
FollowT = t.TypeVar("FollowT", OrganizationFollow, EventSeriesFollow)


def _get_or_reactivate_follow(
    model: type[FollowT],
    user: RevelUser,
    target_field: str,
    target: Model,
    defaults: dict[str, t.Any],
    already_following_message: str,
) -> FollowT:
    """Get or create a follow record, reactivating if archived.

    Args:
        model: The follow model class (OrganizationFollow or EventSeriesFollow)
        user: The user who wants to follow
        target_field: The field name for the target (e.g., "organization" or "event_series")
        target: The target model instance
        defaults: Default values for creation
        already_following_message: Error message if already following

    Returns:
        The created or reactivated follow instance

    Raises:
        HttpError: If already following (and not archived)
    """
    try:
        with transaction.atomic():
            follow, created = model.objects.get_or_create(
                user=user,
                **{target_field: target},
                defaults={**defaults, "is_archived": False},
            )

            if created:
                return follow

            if not follow.is_archived:
                raise HttpError(400, already_following_message)

            # Reactivate archived follow with new preferences
            follow.is_archived = False
            for field, value in defaults.items():
                setattr(follow, field, value)
            follow.save(update_fields=["is_archived", *defaults.keys()])
            return follow
    except IntegrityError:
        raise HttpError(400, already_following_message)


def _archive_follow(
    model: type[FollowT],
    user: RevelUser,
    target_field: str,
    target: Model,
    not_following_message: str,
) -> None:
    """Archive a follow record (soft delete).

    Args:
        model: The follow model class
        user: The user who wants to unfollow
        target_field: The field name for the target
        target: The target model instance
        not_following_message: Error message if not following

    Raises:
        HttpError: If not currently following
    """
    try:
        follow = model.objects.get(user=user, **{target_field: target}, is_archived=False)
    except model.DoesNotExist:
        raise HttpError(400, not_following_message)

    follow.is_archived = True
    follow.save(update_fields=["is_archived"])


def follow_organization(
    user: RevelUser,
    organization: Organization,
    *,
    notify_new_events: bool = True,
    notify_announcements: bool = True,
) -> OrganizationFollow:
    """Follow an organization.

    Args:
        user: The user who wants to follow
        organization: The organization to follow
        notify_new_events: Whether to receive notifications for new events
        notify_announcements: Whether to receive notifications for announcements

    Returns:
        The created or reactivated OrganizationFollow instance

    Raises:
        HttpError: If the organization is not visible to the user
    """
    if not Organization.objects.for_user(user).filter(pk=organization.pk).exists():
        raise HttpError(404, "Organization not found")

    follow = _get_or_reactivate_follow(
        model=OrganizationFollow,
        user=user,
        target_field="organization",
        target=organization,
        defaults={"notify_new_events": notify_new_events, "notify_announcements": notify_announcements},
        already_following_message="Already following this organization",
    )

    # Ensure organization is attached for schema serialization
    follow.organization = organization

    _send_org_follow_notification(user, organization)
    return follow


def _send_org_follow_notification(user: RevelUser, organization: Organization) -> None:
    """Send notification to org admins about a new follower."""

    def send() -> None:
        staff_users = get_staff_for_notification(organization.id, NotificationType.ORGANIZATION_FOLLOWED)
        frontend_base_url = SiteSettings.get_solo().frontend_base_url

        for staff_user in staff_users:
            notification_requested.send(
                sender=OrganizationFollow,
                user=staff_user,
                notification_type=NotificationType.ORGANIZATION_FOLLOWED,
                context={
                    "organization_id": str(organization.id),
                    "organization_name": organization.name,
                    "follower_id": str(user.id),
                    "follower_name": user.display_name,
                    "follower_email": user.email,
                    "frontend_url": f"{frontend_base_url}/org/{organization.slug}",
                },
            )

    transaction.on_commit(send)


def unfollow_organization(user: RevelUser, organization: Organization) -> None:
    """Unfollow an organization (archives the follow record).

    Args:
        user: The user who wants to unfollow
        organization: The organization to unfollow

    Raises:
        HttpError: If not currently following
    """
    _archive_follow(
        model=OrganizationFollow,
        user=user,
        target_field="organization",
        target=organization,
        not_following_message="Not following this organization",
    )


def get_user_followed_organizations(user: RevelUser) -> QuerySet[OrganizationFollow]:
    """Get all organizations followed by a user."""
    return OrganizationFollow.objects.active().for_user(user).with_organization()


def update_organization_follow_preferences(
    user: RevelUser,
    organization: Organization,
    *,
    notify_new_events: bool | None = None,
    notify_announcements: bool | None = None,
) -> OrganizationFollow:
    """Update notification preferences for an organization follow.

    Args:
        user: The user
        organization: The organization
        notify_new_events: New value for notify_new_events (if provided)
        notify_announcements: New value for notify_announcements (if provided)

    Returns:
        Updated OrganizationFollow instance

    Raises:
        HttpError: If not following
    """
    try:
        follow = OrganizationFollow.objects.get(user=user, organization=organization, is_archived=False)
    except OrganizationFollow.DoesNotExist:
        raise HttpError(400, "Not following this organization")

    update_fields: list[str] = []
    if notify_new_events is not None:
        follow.notify_new_events = notify_new_events
        update_fields.append("notify_new_events")
    if notify_announcements is not None:
        follow.notify_announcements = notify_announcements
        update_fields.append("notify_announcements")

    if update_fields:
        follow.save(update_fields=update_fields)

    # Ensure organization is attached for schema serialization
    follow.organization = organization

    return follow


def follow_event_series(
    user: RevelUser,
    event_series: EventSeries,
    *,
    notify_new_events: bool = True,
) -> EventSeriesFollow:
    """Follow an event series.

    Args:
        user: The user who wants to follow
        event_series: The event series to follow
        notify_new_events: Whether to receive notifications for new events

    Returns:
        The created or reactivated EventSeriesFollow instance

    Raises:
        HttpError: If the event series is not visible to the user
    """
    if not EventSeries.objects.for_user(user).filter(pk=event_series.pk).exists():
        raise HttpError(404, "Event series not found")

    follow = _get_or_reactivate_follow(
        model=EventSeriesFollow,
        user=user,
        target_field="event_series",
        target=event_series,
        defaults={"notify_new_events": notify_new_events},
        already_following_message="Already following this series",
    )

    # Ensure event_series is attached for schema serialization
    follow.event_series = event_series

    _send_series_follow_notification(user, event_series)
    return follow


def _send_series_follow_notification(user: RevelUser, event_series: EventSeries) -> None:
    """Send notification to org admins about a new series follower."""

    def send() -> None:
        organization = event_series.organization
        staff_users = get_staff_for_notification(organization.id, NotificationType.EVENT_SERIES_FOLLOWED)
        frontend_base_url = SiteSettings.get_solo().frontend_base_url

        for staff_user in staff_users:
            notification_requested.send(
                sender=EventSeriesFollow,
                user=staff_user,
                notification_type=NotificationType.EVENT_SERIES_FOLLOWED,
                context={
                    "organization_id": str(organization.id),
                    "organization_name": organization.name,
                    "event_series_id": str(event_series.id),
                    "event_series_name": event_series.name,
                    "follower_id": str(user.id),
                    "follower_name": user.display_name,
                    "follower_email": user.email,
                    "frontend_url": f"{frontend_base_url}/org/{organization.slug}/series/{event_series.slug}",
                },
            )

    transaction.on_commit(send)


def unfollow_event_series(user: RevelUser, event_series: EventSeries) -> None:
    """Unfollow an event series (archives the follow record).

    Args:
        user: The user who wants to unfollow
        event_series: The event series to unfollow

    Raises:
        HttpError: If not currently following
    """
    _archive_follow(
        model=EventSeriesFollow,
        user=user,
        target_field="event_series",
        target=event_series,
        not_following_message="Not following this series",
    )


def get_user_followed_event_series(user: RevelUser) -> QuerySet[EventSeriesFollow]:
    """Get all event series followed by a user."""
    return EventSeriesFollow.objects.active().for_user(user).with_event_series()


def update_event_series_follow_preferences(
    user: RevelUser,
    event_series: EventSeries,
    *,
    notify_new_events: bool | None = None,
) -> EventSeriesFollow:
    """Update notification preferences for an event series follow.

    Args:
        user: The user
        event_series: The event series
        notify_new_events: New value for notify_new_events (if provided)

    Returns:
        Updated EventSeriesFollow instance

    Raises:
        HttpError: If not following
    """
    try:
        follow = EventSeriesFollow.objects.get(user=user, event_series=event_series, is_archived=False)
    except EventSeriesFollow.DoesNotExist:
        raise HttpError(400, "Not following this series")

    if notify_new_events is not None:
        follow.notify_new_events = notify_new_events
        follow.save(update_fields=["notify_new_events"])

    # Ensure event_series is attached for schema serialization
    follow.event_series = event_series

    return follow


def get_organization_followers(organization: Organization) -> QuerySet[OrganizationFollow]:
    """Get all followers of an organization."""
    return OrganizationFollow.objects.filter(organization=organization, is_archived=False).select_related("user")


def get_event_series_followers(event_series: EventSeries) -> QuerySet[EventSeriesFollow]:
    """Get all followers of an event series."""
    return EventSeriesFollow.objects.filter(event_series=event_series, is_archived=False).select_related("user")


def is_following_organization(user: RevelUser, organization: Organization) -> bool:
    """Check if a user is following an organization."""
    return OrganizationFollow.objects.filter(user=user, organization=organization, is_archived=False).exists()


def is_following_event_series(user: RevelUser, event_series: EventSeries) -> bool:
    """Check if a user is following an event series."""
    return EventSeriesFollow.objects.filter(user=user, event_series=event_series, is_archived=False).exists()


def get_followers_for_new_event_notification(
    organization: Organization,
    event_series: EventSeries | None = None,
) -> t.Iterator[tuple[RevelUser, NotificationType]]:
    """Get users to notify about a new event, yielding (user, notification_type) pairs.

    This function returns followers who have opted in to receive new event notifications.
    It yields each user only once, prioritizing series follows over org follows.

    IMPORTANT: Members are excluded because they already receive EVENT_OPEN notifications
    via the membership-based notification system. This prevents duplicate notifications.

    Args:
        organization: The organization that created the event
        event_series: The event series (if the event belongs to one)

    Yields:
        Tuples of (user, notification_type) to notify
    """
    # Get member user IDs to exclude - they already get EVENT_OPEN notifications
    member_user_ids: set[UUID] = set(
        OrganizationMember.objects.filter(
            organization=organization,
            status__in=[
                OrganizationMember.MembershipStatus.ACTIVE,
                OrganizationMember.MembershipStatus.PAUSED,
            ],
        ).values_list("user_id", flat=True)
    )

    notified_user_ids: set[UUID] = set()

    # First, notify series followers (if event is in a series)
    if event_series:
        series_followers = EventSeriesFollow.objects.filter(
            event_series=event_series,
            is_archived=False,
            notify_new_events=True,
        ).select_related("user")

        for follow in series_followers:
            # Skip members - they get EVENT_OPEN notification instead
            if follow.user_id in member_user_ids:
                continue
            if follow.user_id not in notified_user_ids:
                notified_user_ids.add(follow.user_id)
                yield follow.user, NotificationType.NEW_EVENT_FROM_FOLLOWED_SERIES

    # Then, notify org followers (excluding those already notified via series)
    org_followers = OrganizationFollow.objects.filter(
        organization=organization,
        is_archived=False,
        notify_new_events=True,
    ).select_related("user")

    for org_follow in org_followers:
        # Skip members - they get EVENT_OPEN notification instead
        if org_follow.user_id in member_user_ids:
            continue
        if org_follow.user_id not in notified_user_ids:
            notified_user_ids.add(org_follow.user_id)
            yield org_follow.user, NotificationType.NEW_EVENT_FROM_FOLLOWED_ORG
