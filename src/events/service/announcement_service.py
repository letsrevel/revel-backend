"""Service layer for organization announcements.

This module handles announcement CRUD operations and notification delivery.
"""

import datetime as dt
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
    # Validate event belongs to organization if specified. Recurring-series
    # template events are excluded: they are internal blueprints and must not
    # be targetable by user-facing announcements.
    event: Event | None = None
    if payload.event_id:
        event = (
            Event.objects.exclude_templates()
            .filter(
                id=payload.event_id,
                organization=organization,
            )
            .first()
        )
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


def _apply_event_id_update(announcement: Announcement, event_id: UUID | None) -> None:
    """Resolve and apply an ``event_id`` update on an announcement.

    Recurring-series template events are excluded: they are internal blueprints
    and must not be targetable.

    Args:
        announcement: Announcement being updated.
        event_id: New event id, or ``None`` to clear the event target.

    Raises:
        ValueError: If the event does not exist or belongs to another organization.
    """
    if event_id is None:
        announcement.event = None
        return
    event = (
        Event.objects.exclude_templates()
        .filter(
            id=event_id,
            organization=announcement.organization,
        )
        .first()
    )
    if not event:
        raise ValueError(_ERR_EVENT_NOT_FOUND)
    announcement.event = event


def update_announcement(
    announcement: Announcement,
    payload: AnnouncementUpdateSchema,
) -> Announcement:
    """Update a draft or scheduled announcement.

    Args:
        announcement: Announcement to update (must be in DRAFT or SCHEDULED status).
        payload: Validated update data.

    Returns:
        Updated announcement instance.

    Raises:
        ValueError: If announcement is neither a draft nor scheduled.
    """
    editable = (Announcement.AnnouncementStatus.DRAFT, Announcement.AnnouncementStatus.SCHEDULED)
    if announcement.status not in editable:
        raise ValueError("Only draft or scheduled announcements can be updated")

    update_data = payload.model_dump(exclude_unset=True)

    if "event_id" in update_data:
        _apply_event_id_update(announcement, update_data.pop("event_id"))

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

    if announcement.resend_to_new_signups and announcement.event_id is None:
        raise ValueError("Re-sending to new sign-ups requires an event-targeted announcement")

    if announcement.schedule_anchor is not None and announcement.event_id is None:
        raise ValueError("Relative scheduling requires an event-targeted announcement")

    # Validate that at least one targeting option remains
    _validate_announcement_has_targeting(announcement)

    announcement.save()

    logger.info(
        "announcement_updated",
        announcement_id=str(announcement.id),
    )

    return announcement


def schedule_announcement(
    announcement: Announcement,
    *,
    scheduled_at: dt.datetime | None = None,
    schedule_anchor: Announcement.ScheduleAnchor | None = None,
    schedule_offset_minutes: int | None = None,
) -> Announcement:
    """Move a DRAFT announcement to SCHEDULED.

    Provide either an absolute ``scheduled_at`` or a relative
    (``schedule_anchor`` + ``schedule_offset_minutes``) schedule. The resolved
    send time must be in the future.

    Args:
        announcement: Draft announcement to schedule.
        scheduled_at: Absolute send time (mutually exclusive with relative scheduling).
        schedule_anchor: Anchor for a relative schedule (event start or end).
        schedule_offset_minutes: Signed minutes from the anchor.

    Returns:
        The scheduled announcement.

    Raises:
        ValueError: If not a draft, the schedule is unresolvable, or it is in the past.
    """
    if announcement.status != Announcement.AnnouncementStatus.DRAFT:
        raise ValueError("Only draft announcements can be scheduled")

    announcement.scheduled_at = scheduled_at
    announcement.schedule_anchor = schedule_anchor
    announcement.schedule_offset_minutes = schedule_offset_minutes

    is_relative = announcement.schedule_anchor is not None or announcement.schedule_offset_minutes is not None
    if is_relative and (announcement.schedule_anchor is None or announcement.schedule_offset_minutes is None):
        raise ValueError("Relative scheduling requires both an anchor and an offset")

    resolved = announcement.effective_send_at
    if resolved is None:
        raise ValueError("Could not resolve a scheduled time (relative scheduling requires an event)")
    if resolved <= timezone.now():
        raise ValueError("Scheduled time must be in the future")

    announcement.status = Announcement.AnnouncementStatus.SCHEDULED
    announcement.save()
    logger.info("announcement_scheduled", announcement_id=str(announcement.id), send_at=resolved.isoformat())
    return announcement


def unschedule_announcement(announcement: Announcement) -> Announcement:
    """Revert a SCHEDULED announcement to DRAFT and clear its schedule.

    Args:
        announcement: Scheduled announcement to revert.

    Returns:
        The reverted draft announcement.

    Raises:
        ValueError: If the announcement is not scheduled.
    """
    if announcement.status != Announcement.AnnouncementStatus.SCHEDULED:
        raise ValueError("Only scheduled announcements can be unscheduled")

    announcement.status = Announcement.AnnouncementStatus.DRAFT
    announcement.scheduled_at = None
    announcement.schedule_anchor = None
    announcement.schedule_offset_minutes = None
    announcement.save()
    logger.info("announcement_unscheduled", announcement_id=str(announcement.id))
    return announcement


def get_recipients(announcement: Announcement) -> QuerySet[RevelUser]:
    """Get the recipients for an announcement.

    Determines recipients based on targeting options:
    - event: Event attendees (active/checked-in tickets, YES RSVPs)
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
    - Users with YES RSVPs

    Args:
        event: Event to get attendees for.

    Returns:
        QuerySet of users who are event attendees.
    """
    # Get ticket holder user IDs (ACTIVE and CHECKED_IN tickets)
    # Note: AT_THE_DOOR tickets are now created as ACTIVE, so no special handling needed
    active_ticket_statuses = [Ticket.TicketStatus.ACTIVE, Ticket.TicketStatus.CHECKED_IN]
    ticket_user_ids = set(
        Ticket.objects.filter(
            event=event,
            status__in=active_ticket_statuses,
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
    all_user_ids = ticket_user_ids | rsvp_user_ids

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
    """Send a draft or scheduled announcement to all recipients.

    Locks the row, snapshots recipients, marks the announcement SENT, and
    dispatches notifications. Accepts DRAFT (manual send) or SCHEDULED (beat sweep).

    Args:
        announcement: Announcement to send (must be DRAFT or SCHEDULED).

    Returns:
        Number of notifications created.

    Raises:
        ValueError: If the announcement is not DRAFT or SCHEDULED.
    """
    announcement = Announcement.objects.select_related("organization").select_for_update().get(pk=announcement.pk)

    sendable = (Announcement.AnnouncementStatus.DRAFT, Announcement.AnnouncementStatus.SCHEDULED)
    if announcement.status not in sendable:
        raise ValueError("Only draft or scheduled announcements can be sent")

    recipients = list(get_recipients(announcement).select_related("notification_preferences"))
    recipient_count = len(recipients)

    announcement.status = Announcement.AnnouncementStatus.SENT
    announcement.sent_at = timezone.now()
    announcement.recipient_count = recipient_count
    announcement.save(update_fields=["status", "sent_at", "recipient_count"])

    if recipient_count == 0:
        logger.info("announcement_sent_no_recipients", announcement_id=str(announcement.id))
        return 0

    _deliver_to_recipients(announcement, recipients)
    logger.info("announcement_sent", announcement_id=str(announcement.id), recipient_count=recipient_count)
    return recipient_count


@transaction.atomic
def resend_to_new_recipients(announcement: Announcement) -> int:
    """Re-deliver a sent announcement to attendees who joined after it was sent.

    Computes the delta = current recipients MINUS users who already hold an
    ``ORG_ANNOUNCEMENT`` notification for this announcement (the dedup ledger),
    then delivers to and counts only the new users. The caller (beat sweep) is
    responsible for excluding announcements whose event has ended.

    Args:
        announcement: A SENT announcement configured for resending.

    Returns:
        Number of new recipients notified.

    Raises:
        ValueError: If the announcement is not SENT or not configured for resending.
    """
    from notifications.models import Notification

    announcement = Announcement.objects.select_related("organization").select_for_update().get(pk=announcement.pk)
    if announcement.status != Announcement.AnnouncementStatus.SENT:
        raise ValueError("Only sent announcements can be resent")
    if not announcement.resend_to_new_signups:
        raise ValueError("Announcement is not configured for resending")

    current_ids = set(get_recipients(announcement).values_list("id", flat=True))
    notified_ids = set(
        Notification.objects.filter(
            notification_type=NotificationType.ORG_ANNOUNCEMENT,
            context__announcement_id=str(announcement.id),
        ).values_list("user_id", flat=True)
    )
    new_ids = current_ids - notified_ids
    if not new_ids:
        return 0

    recipients = list(RevelUser.objects.filter(id__in=new_ids).select_related("notification_preferences"))
    sent = _deliver_to_recipients(announcement, recipients)
    if sent:
        announcement.recipient_count = announcement.recipient_count + sent
        announcement.save(update_fields=["recipient_count"])
    logger.info("announcement_resent_to_new_signups", announcement_id=str(announcement.id), new_recipients=sent)
    return sent


def _deliver_to_recipients(announcement: Announcement, recipients: list[RevelUser]) -> int:
    """Create notifications for the given recipients and dispatch them.

    Shared by the initial send, the scheduled send, and the resend-to-new-signups flow.

    Args:
        announcement: Announcement providing the notification context.
        recipients: Users to notify (already de-duplicated).

    Returns:
        Number of notifications created.
    """
    from notifications.tasks import dispatch_notifications_batch

    if not recipients:
        return 0

    context = _build_notification_context(announcement)
    notifications_data: list[NotificationData] = [
        NotificationData(
            notification_type=NotificationType.ORG_ANNOUNCEMENT,
            user=user,
            context=context,
        )
        for user in recipients
    ]
    created_notifications = bulk_create_notifications(notifications_data)
    notification_ids = [str(n.id) for n in created_notifications]
    transaction.on_commit(lambda: dispatch_notifications_batch.delay(notification_ids))
    return len(created_notifications)


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
