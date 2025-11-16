"""Eligibility service for determining who should receive notifications.

This module contains functions for checking user participation in events/organizations
and determining which users should receive specific notification types.
"""

import logging
from uuid import UUID

from django.db.models import Q, QuerySet

from accounts.models import RevelUser
from events.models import Event, EventInvitation, EventRSVP, Organization, OrganizationMember, Ticket, TicketTier
from notifications.enums import NotificationType
from notifications.models import NotificationPreference

logger = logging.getLogger(__name__)


def has_active_rsvp(user: RevelUser, event: Event) -> bool:
    """Check if user has an active RSVP (YES or MAYBE) for an event.

    Args:
        user: The user to check
        event: The event to check

    Returns:
        True if user has RSVP'd YES or MAYBE
    """
    return EventRSVP.objects.filter(
        user=user, event=event, status__in=[EventRSVP.RsvpStatus.YES, EventRSVP.RsvpStatus.MAYBE]
    ).exists()


def has_active_ticket(user: RevelUser, event: Event) -> bool:
    """Check if user has an active or pending ticket for an event.

    For online payment tiers: only ACTIVE tickets count.
    For offline payment tiers: ACTIVE or PENDING tickets count.

    Args:
        user: The user to check
        event: The event to check

    Returns:
        True if user has a valid ticket
    """
    tickets = Ticket.objects.filter(user=user, event=event).select_related("tier")

    for ticket in tickets:
        if ticket.status == Ticket.TicketStatus.ACTIVE:
            return True

        # For offline payment tiers, pending tickets also count
        if (
            ticket.tier
            and ticket.tier.payment_method != TicketTier.PaymentMethod.ONLINE
            and ticket.status == Ticket.TicketStatus.PENDING
        ):
            return True

    return False


def has_event_invitation(user: RevelUser, event: Event) -> bool:
    """Check if user has been invited to an event.

    Args:
        user: The user to check
        event: The event to check

    Returns:
        True if user has an invitation
    """
    return EventInvitation.objects.filter(user=user, event=event).exists()


def is_org_member(user: RevelUser, organization: Organization) -> bool:
    """Check if user is a member of an organization.

    Args:
        user: The user to check
        organization: The organization to check

    Returns:
        True if user is a member
    """
    return OrganizationMember.objects.filter(user=user, organization=organization).exists()


def is_org_staff(user: RevelUser, organization: Organization) -> bool:
    """Check if user is staff or owner of an organization.

    Args:
        user: The user to check
        organization: The organization to check

    Returns:
        True if user is staff or owner
    """
    return organization.owner_id == user.id or organization.staff_members.filter(id=user.id).exists()


def is_participating_in_event(user: RevelUser, event: Event) -> bool:
    """Check if user is actively participating in an event.

    User is participating if they:
    1. Are organization owner/staff, OR
    2. Have RSVP'd (YES or MAYBE), OR
    3. Have an active/pending ticket, OR
    4. Have been explicitly invited

    Args:
        user: The user to check
        event: The event to check

    Returns:
        True if user is participating
    """
    return (
        is_org_staff(user, event.organization)
        or has_active_rsvp(user, event)
        or has_active_ticket(user, event)
        or has_event_invitation(user, event)
    )


def is_user_eligible_for_notification(
    user: RevelUser,
    notification_type: NotificationType,
    *,
    event: Event | None = None,
    organization: Organization | None = None,
) -> bool:
    """Check if a user is eligible to receive a specific notification.

    Eligibility is based on:
    1. User's notification preferences (not silenced globally)
    2. Notification type is enabled in user's preferences
    3. User is participating in the event/organization

    Args:
        user: The user to check
        notification_type: The type of notification
        event: Optional event context
        organization: Optional organization context

    Returns:
        True if user should receive the notification
    """
    # Get user's notification preferences
    prefs, _ = NotificationPreference.objects.get_or_create(user=user)

    # Check global silence
    if prefs.silence_all_notifications:
        return False

    # Check if notification type is enabled
    if not prefs.is_notification_type_enabled(notification_type):
        return False

    # Check participation
    if event:
        # For EVENT_OPEN notifications, org members are eligible even without other participation
        if notification_type == NotificationType.EVENT_OPEN:
            return is_org_member(user, event.organization) or is_participating_in_event(user, event)
        return is_participating_in_event(user, event)

    if organization:
        return is_org_member(user, organization)

    return False


def get_eligible_users_for_event_notification(event: Event, notification_type: NotificationType) -> QuerySet[RevelUser]:
    """Get users eligible to receive notifications for an event.

    Eligibility is based on actual participation AND event visibility:
    - STAFF_ONLY: Only org staff and owners
    - MEMBERS_ONLY: Org staff, owners, members, and users with explicit participation
    - PRIVATE: Org staff, owners, and users with explicit participation (invitations, tickets, RSVPs)
    - PUBLIC: Anyone with any form of participation

    Args:
        event: The event for which to send notifications
        notification_type: The type of notification being sent

    Returns:
        QuerySet of eligible users
    """
    # Build query for participating users
    participants_q = Q()

    # Organization staff and owners (for all event notifications and all visibility levels)
    staff_and_owners_q = Q(owned_organizations=event.organization_id) | Q(
        organization_staff_memberships__organization_id=event.organization_id
    )
    participants_q |= staff_and_owners_q

    # Apply visibility-specific rules
    if event.visibility == Event.Visibility.STAFF_ONLY:
        # Only staff and owners (already added above)
        pass

    elif event.visibility == Event.Visibility.MEMBERS_ONLY:
        # Include org members
        participants_q |= Q(organization_memberships__organization_id=event.organization_id)

        # Include users with explicit participation
        participants_q |= Q(
            rsvps__event=event, rsvps__status__in=[EventRSVP.RsvpStatus.YES, EventRSVP.RsvpStatus.MAYBE]
        )
        participants_q |= Q(
            tickets__event=event, tickets__status__in=[Ticket.TicketStatus.ACTIVE, Ticket.TicketStatus.PENDING]
        )
        participants_q |= Q(invitations__event=event)

    elif event.visibility == Event.Visibility.PRIVATE:
        # Only explicitly invited/participating users (not all org members)
        participants_q |= Q(
            rsvps__event=event, rsvps__status__in=[EventRSVP.RsvpStatus.YES, EventRSVP.RsvpStatus.MAYBE]
        )
        participants_q |= Q(
            tickets__event=event, tickets__status__in=[Ticket.TicketStatus.ACTIVE, Ticket.TicketStatus.PENDING]
        )
        participants_q |= Q(invitations__event=event)

    elif event.visibility == Event.Visibility.PUBLIC:
        # For EVENT_OPEN notifications, include all org members
        if notification_type == NotificationType.EVENT_OPEN:
            participants_q |= Q(organization_memberships__organization_id=event.organization_id)

        # Include users with explicit participation
        participants_q |= Q(
            rsvps__event=event, rsvps__status__in=[EventRSVP.RsvpStatus.YES, EventRSVP.RsvpStatus.MAYBE]
        )
        participants_q |= Q(
            tickets__event=event, tickets__status__in=[Ticket.TicketStatus.ACTIVE, Ticket.TicketStatus.PENDING]
        )
        participants_q |= Q(invitations__event=event)

    # Filter out users who have silenced all notifications early to reduce Python iteration
    participants_with_prefs_q = participants_q & ~Q(notification_preferences__silence_all_notifications=True)

    # Get unique users with preferences prefetched to avoid N+1 queries
    potential_users = (
        RevelUser.objects.filter(participants_with_prefs_q)
        .select_related("notification_preferences")
        .distinct()
    )

    # Filter based on notification preferences and participation rules
    eligible_user_ids: list[UUID] = []

    for user in potential_users:
        if is_user_eligible_for_notification(user, notification_type, event=event):
            eligible_user_ids.append(user.id)

    return RevelUser.objects.filter(id__in=eligible_user_ids)


def get_organization_staff_and_owners(organization_id: UUID) -> QuerySet[RevelUser]:
    """Get all staff members and owners of an organization.

    Args:
        organization_id: The organization ID

    Returns:
        QuerySet of staff and owner users with notification preferences prefetched
    """
    return (
        RevelUser.objects.filter(
            Q(owned_organizations=organization_id) | Q(organization_staff_memberships__organization_id=organization_id)
        )
        .select_related("notification_preferences")
        .distinct()
    )


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
    return is_user_eligible_for_notification(user, notification_type, organization=organization)


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
