"""Service layer for organization announcements.

This module handles announcement CRUD operations and notification delivery.
"""

import typing as t
from uuid import UUID

import structlog
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import (
    Announcement,
    Event,
    EventRSVP,
    MembershipTier,
    Organization,
    OrganizationMember,
    Ticket,
    TicketTier,
)
from events.schema.announcement import AnnouncementCreateSchema, AnnouncementUpdateSchema
from notifications.enums import NotificationType
from notifications.service.dispatcher import NotificationData, bulk_create_notifications

logger = structlog.get_logger(__name__)

# Error messages
_ERR_EVENT_NOT_FOUND = "Event not found or does not belong to this organization"


def _validate_announcement_has_targeting(announcement: Announcement) -> None:
    """Ensure announcement has at least one targeting option.

    Args:
        announcement: Announcement to validate.

    Raises:
        ValueError: If no targeting option is active.
    """
    has_targeting = any(
        [
            announcement.event_id is not None,
            announcement.target_all_members,
            announcement.target_tiers.exists(),
            announcement.target_staff_only,
        ]
    )
    if not has_targeting:
        raise ValueError(
            "Announcement must have at least one targeting option: "
            "event, target_all_members, target_tiers, or target_staff_only"
        )


def create_announcement(
    organization: Organization,
    user: RevelUser,
    payload: AnnouncementCreateSchema,
) -> Announcement:
    """Create a draft announcement.

    Args:
        organization: Organization creating the announcement.
        user: User creating the announcement.
        payload: Validated announcement data.

    Returns:
        Created announcement instance.
    """
    # Validate event belongs to organization if specified
    event: Event | None = None
    if payload.event_id:
        event = Event.objects.filter(
            id=payload.event_id,
            organization=organization,
        ).first()
        if not event:
            raise ValueError(_ERR_EVENT_NOT_FOUND)

    # Create announcement
    announcement = Announcement.objects.create(
        organization=organization,
        event=event,
        title=payload.title,
        body=payload.body,
        target_all_members=payload.target_all_members,
        target_staff_only=payload.target_staff_only,
        past_visibility=payload.past_visibility,
        created_by=user,
        status=Announcement.AnnouncementStatus.DRAFT,
    )

    # Handle target tiers
    if payload.target_tier_ids:
        tiers = MembershipTier.objects.filter(
            id__in=payload.target_tier_ids,
            organization=organization,
        )
        announcement.target_tiers.set(tiers)

    logger.info(
        "announcement_created",
        announcement_id=str(announcement.id),
        organization_id=str(organization.id),
        created_by=str(user.id),
    )

    return announcement


def update_announcement(
    announcement: Announcement,
    payload: AnnouncementUpdateSchema,
) -> Announcement:
    """Update a draft announcement.

    Args:
        announcement: Announcement to update (must be in DRAFT status).
        payload: Validated update data.

    Returns:
        Updated announcement instance.

    Raises:
        ValueError: If announcement is not a draft.
    """
    if announcement.status != Announcement.AnnouncementStatus.DRAFT:
        raise ValueError("Only draft announcements can be updated")

    update_data = payload.model_dump(exclude_unset=True)

    # Handle event_id specially
    if "event_id" in update_data:
        event_id = update_data.pop("event_id")
        if event_id is None:
            announcement.event = None
        else:
            event = Event.objects.filter(
                id=event_id,
                organization=announcement.organization,
            ).first()
            if not event:
                raise ValueError(_ERR_EVENT_NOT_FOUND)
            announcement.event = event

    # Handle target_tier_ids specially
    if "target_tier_ids" in update_data:
        tier_ids = update_data.pop("target_tier_ids")
        if tier_ids is None or len(tier_ids) == 0:
            announcement.target_tiers.clear()
        else:
            tiers = MembershipTier.objects.filter(
                id__in=tier_ids,
                organization=announcement.organization,
            )
            announcement.target_tiers.set(tiers)

    # Update remaining fields
    for field, value in update_data.items():
        if value is not None:
            setattr(announcement, field, value)

    # Validate that at least one targeting option remains
    _validate_announcement_has_targeting(announcement)

    announcement.save()

    logger.info(
        "announcement_updated",
        announcement_id=str(announcement.id),
    )

    return announcement


def get_recipients(announcement: Announcement) -> QuerySet[RevelUser]:
    """Get the recipients for an announcement.

    Determines recipients based on targeting options:
    - event: Event attendees (active/checked-in tickets, AT_THE_DOOR pending, YES RSVPs)
    - target_all_members: All active organization members
    - target_tiers: Members of specified membership tiers
    - target_staff_only: Organization staff members

    Args:
        announcement: Announcement to get recipients for.

    Returns:
        QuerySet of RevelUser instances who should receive the announcement.
    """
    if announcement.event:
        return _get_event_recipients(announcement.event)

    if announcement.target_all_members:
        return _get_all_members_recipients(announcement.organization)

    # Check target_tiers - need to evaluate if any exist
    target_tier_ids = list(announcement.target_tiers.values_list("id", flat=True))
    if target_tier_ids:
        return _get_tier_recipients(announcement.organization, target_tier_ids)

    if announcement.target_staff_only:
        return _get_staff_recipients(announcement.organization)

    return RevelUser.objects.none()


def _get_event_recipients(event: Event) -> QuerySet[RevelUser]:
    """Get recipients for an event-targeted announcement.

    Includes:
    - Users with active or checked-in tickets
    - Users with pending AT_THE_DOOR tickets
    - Users with YES RSVPs

    Args:
        event: Event to get attendees for.

    Returns:
        QuerySet of users who are event attendees.
    """
    # Get ticket holder user IDs
    active_ticket_statuses = [Ticket.TicketStatus.ACTIVE, Ticket.TicketStatus.CHECKED_IN]
    ticket_user_ids = set(
        Ticket.objects.filter(
            event=event,
            status__in=active_ticket_statuses,
        ).values_list("user_id", flat=True)
    )

    # Include AT_THE_DOOR pending tickets
    at_door_pending_user_ids = set(
        Ticket.objects.filter(
            event=event,
            status=Ticket.TicketStatus.PENDING,
            tier__payment_method=TicketTier.PaymentMethod.AT_THE_DOOR,
        ).values_list("user_id", flat=True)
    )

    # Get RSVP user IDs
    rsvp_user_ids = set(
        EventRSVP.objects.filter(
            event=event,
            status=EventRSVP.RsvpStatus.YES,
        ).values_list("user_id", flat=True)
    )

    # Combine all user IDs
    all_user_ids = ticket_user_ids | at_door_pending_user_ids | rsvp_user_ids

    if not all_user_ids:
        return RevelUser.objects.none()

    return RevelUser.objects.filter(id__in=all_user_ids)


def _get_all_members_recipients(organization: Organization) -> QuerySet[RevelUser]:
    """Get all active members of an organization.

    Args:
        organization: Organization to get members for.

    Returns:
        QuerySet of active member users.
    """
    return RevelUser.objects.filter(
        organization_memberships__organization=organization,
        organization_memberships__status=OrganizationMember.MembershipStatus.ACTIVE,
    ).distinct()


def _get_tier_recipients(
    organization: Organization,
    tier_ids: list[UUID],
) -> QuerySet[RevelUser]:
    """Get members of specific membership tiers.

    Args:
        organization: Organization to get members for.
        tier_ids: List of tier IDs to include.

    Returns:
        QuerySet of members in the specified tiers.
    """
    return RevelUser.objects.filter(
        organization_memberships__organization=organization,
        organization_memberships__status=OrganizationMember.MembershipStatus.ACTIVE,
        organization_memberships__tier_id__in=tier_ids,
    ).distinct()


def _get_staff_recipients(organization: Organization) -> QuerySet[RevelUser]:
    """Get staff members of an organization.

    Args:
        organization: Organization to get staff for.

    Returns:
        QuerySet of staff member users.
    """
    return RevelUser.objects.filter(
        organization_staff_memberships__organization=organization,
    ).distinct()


def get_recipient_count(announcement: Announcement) -> int:
    """Get the count of recipients for an announcement.

    This is useful for previewing before sending.

    Args:
        announcement: Announcement to count recipients for.

    Returns:
        Number of users who would receive the announcement.
    """
    return get_recipients(announcement).count()


@transaction.atomic
def send_announcement(announcement: Announcement) -> int:
    """Send an announcement to all recipients.

    Creates notifications for all recipients and dispatches them.
    Updates the announcement status to SENT.

    Uses database-level locking to prevent race conditions where multiple
    concurrent requests could send the same announcement twice.

    Args:
        announcement: Announcement to send (must be in DRAFT status).

    Returns:
        Number of notifications sent.

    Raises:
        ValueError: If announcement is not a draft.
    """
    from notifications.tasks import dispatch_notifications_batch

    # Lock the announcement row to prevent concurrent sends
    announcement = Announcement.objects.select_for_update().get(pk=announcement.pk)

    if announcement.status != Announcement.AnnouncementStatus.DRAFT:
        raise ValueError("Only draft announcements can be sent")

    # Get all recipients with notification preferences prefetched
    recipients = list(get_recipients(announcement).select_related("notification_preferences"))
    recipient_count = len(recipients)

    # Update announcement status even if no recipients
    announcement.status = Announcement.AnnouncementStatus.SENT
    announcement.sent_at = timezone.now()
    announcement.recipient_count = recipient_count
    announcement.save(update_fields=["status", "sent_at", "recipient_count"])

    if recipient_count == 0:
        logger.info(
            "announcement_sent_no_recipients",
            announcement_id=str(announcement.id),
        )
        return 0

    # Build notification context
    context = _build_notification_context(announcement)

    # Create notifications for all recipients
    notifications_data: list[NotificationData] = [
        NotificationData(
            notification_type=NotificationType.ORG_ANNOUNCEMENT,
            user=user,
            context=context,
        )
        for user in recipients
    ]

    # Bulk create all notifications (single INSERT)
    created_notifications = bulk_create_notifications(notifications_data)

    # Dispatch all notifications in a batch task
    notification_ids = [str(n.id) for n in created_notifications]
    dispatch_notifications_batch.delay(notification_ids)

    logger.info(
        "announcement_sent",
        announcement_id=str(announcement.id),
        recipient_count=recipient_count,
    )

    return recipient_count


def _build_notification_context(announcement: Announcement) -> dict[str, t.Any]:
    """Build the notification context for an announcement.

    Args:
        announcement: Announcement to build context for.

    Returns:
        Context dictionary for the notification.
    """
    frontend_base_url = SiteSettings.get_solo().frontend_base_url

    context: dict[str, t.Any] = {
        "organization_id": str(announcement.organization.id),
        "organization_name": announcement.organization.name,
        "organization_url": f"{frontend_base_url}/org/{announcement.organization.slug}",
        "announcement_id": str(announcement.id),
        "announcement_title": announcement.title,
        "announcement_body": announcement.body,
        "posted_by_name": (announcement.created_by.display_name if announcement.created_by else ""),
    }

    if announcement.event:
        context["event_id"] = str(announcement.event.id)
        context["event_name"] = announcement.event.name
        context["event_url"] = f"{frontend_base_url}/events/{announcement.event.id}"

    return context


def is_user_eligible_for_announcement(
    announcement: Announcement,
    user: RevelUser,
) -> bool:
    """Check if a user is eligible to see an announcement.

    This is used for visibility checks on public endpoints.

    Note:
        This function performs 1-2 database queries per call (notification check,
        and optionally recipient eligibility check if past_visibility is enabled).
        When used in a loop over multiple announcements, this results in N+1 queries.
        For the current use case (small announcement lists per org/event), this is
        acceptable. If performance becomes an issue with large announcement volumes,
        consider batch-prefetching notification existence.

    Args:
        announcement: Announcement to check.
        user: User to check eligibility for.

    Returns:
        True if user can see the announcement.
    """
    if announcement.status != Announcement.AnnouncementStatus.SENT:
        return False

    # Check if user was a recipient (received notification)
    from notifications.models import Notification

    received_notification = Notification.objects.filter(
        user=user,
        notification_type=NotificationType.ORG_ANNOUNCEMENT,
        context__announcement_id=str(announcement.id),
    ).exists()

    if received_notification:
        return True

    # If past_visibility is enabled, check current eligibility
    if announcement.past_visibility:
        return get_recipients(announcement).filter(id=user.id).exists()

    return False
