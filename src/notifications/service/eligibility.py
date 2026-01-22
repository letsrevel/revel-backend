"""Eligibility service for determining who should receive notifications.

This module contains functions for checking user participation in events/organizations
and determining which users should receive specific notification types.
"""

from uuid import UUID

import structlog
from django.db.models import Q, QuerySet

from accounts.models import RevelUser
from events.models import Event, EventInvitation, EventRSVP, Organization, OrganizationMember, Ticket, TicketTier
from notifications.enums import NotificationType
from notifications.models import NotificationPreference

logger = structlog.get_logger(__name__)


class BatchParticipationChecker:
    """Batch checker for user participation in an event.

    Prefetches all participation data (RSVPs, tickets, invitations, staff/owner status)
    in a few queries upfront, then provides O(1) lookups for individual users.

    This avoids N+1 queries when checking participation for many users.
    """

    def __init__(self, event: Event) -> None:
        """Initialize with an event and prefetch all participation data.

        Args:
            event: The event to check participation for
        """
        self.event = event
        self.organization = event.organization
        self._prefetch_data()

    def _prefetch_data(self) -> None:
        """Prefetch all participation data in batch queries."""
        # Staff and owner IDs (owner is always staff-equivalent)
        self.staff_user_ids: set[UUID] = set(self.organization.staff_members.values_list("id", flat=True))
        self.staff_user_ids.add(self.organization.owner_id)

        # Active RSVPs (YES or MAYBE)
        self.rsvp_user_ids: set[UUID] = set(
            EventRSVP.objects.filter(
                event=self.event,
                status__in=[EventRSVP.RsvpStatus.YES, EventRSVP.RsvpStatus.MAYBE],
            ).values_list("user_id", flat=True)
        )

        # Active tickets - need to handle payment method logic
        # ACTIVE tickets always count, PENDING only for non-ONLINE payment methods
        self.ticket_user_ids: set[UUID] = set(
            Ticket.objects.filter(
                event=self.event,
                status=Ticket.TicketStatus.ACTIVE,
            ).values_list("user_id", flat=True)
        )
        # Add users with PENDING tickets for non-ONLINE payment methods
        pending_offline_ticket_users = set(
            Ticket.objects.filter(
                event=self.event,
                status=Ticket.TicketStatus.PENDING,
            )
            .exclude(tier__payment_method=TicketTier.PaymentMethod.ONLINE)
            .values_list("user_id", flat=True)
        )
        self.ticket_user_ids |= pending_offline_ticket_users

        # Event invitations
        self.invitation_user_ids: set[UUID] = set(
            EventInvitation.objects.filter(event=self.event).values_list("user_id", flat=True)
        )

        # Organization members (for EVENT_OPEN notifications)
        self.member_user_ids: set[UUID] = set(
            OrganizationMember.objects.filter(
                organization=self.organization,
            ).values_list("user_id", flat=True)
        )

    def is_org_staff(self, user_id: UUID) -> bool:
        """Check if user is staff or owner of the organization.

        Args:
            user_id: The user ID to check

        Returns:
            True if user is staff or owner
        """
        return user_id in self.staff_user_ids

    def has_active_rsvp(self, user_id: UUID) -> bool:
        """Check if user has an active RSVP (YES or MAYBE).

        Args:
            user_id: The user ID to check

        Returns:
            True if user has RSVP'd YES or MAYBE
        """
        return user_id in self.rsvp_user_ids

    def has_active_ticket(self, user_id: UUID) -> bool:
        """Check if user has an active or valid pending ticket.

        Args:
            user_id: The user ID to check

        Returns:
            True if user has a valid ticket
        """
        return user_id in self.ticket_user_ids

    def has_event_invitation(self, user_id: UUID) -> bool:
        """Check if user has been invited to the event.

        Args:
            user_id: The user ID to check

        Returns:
            True if user has an invitation
        """
        return user_id in self.invitation_user_ids

    def is_org_member(self, user_id: UUID) -> bool:
        """Check if user is a member of the organization.

        Args:
            user_id: The user ID to check

        Returns:
            True if user is a member
        """
        return user_id in self.member_user_ids

    def is_participating(self, user_id: UUID) -> bool:
        """Check if user is actively participating in the event.

        User is participating if they:
        1. Are organization owner/staff, OR
        2. Have RSVP'd (YES or MAYBE), OR
        3. Have an active/pending ticket, OR
        4. Have been explicitly invited

        Args:
            user_id: The user ID to check

        Returns:
            True if user is participating
        """
        return (
            self.is_org_staff(user_id)
            or self.has_active_rsvp(user_id)
            or self.has_active_ticket(user_id)
            or self.has_event_invitation(user_id)
        )

    def can_see_address(self, user_id: UUID) -> bool:
        """Check if user can see the event address based on address_visibility.

        Uses the same visibility rules as Event.can_user_see_address but with O(1) set lookups:
        - PUBLIC: Everyone can see
        - PRIVATE: Invited users, ticket holders, or RSVPs
        - MEMBERS_ONLY: Organization members
        - STAFF_ONLY: Only staff/owners
        - ATTENDEES_ONLY: Only ticket holders or RSVPs (confirmed attendance)

        Note: This does NOT check is_superuser/is_staff on the User model.
        For notification loops, we assume regular users. If you need to handle
        Django superusers/staff, check those flags separately before calling this.

        Args:
            user_id: The user ID to check

        Returns:
            True if user can see the event address
        """
        from events.models.mixins import ResourceVisibility

        address_visibility = self.event.address_visibility

        # PUBLIC: Everyone can see
        if address_visibility == ResourceVisibility.PUBLIC:
            return True

        # STAFF_ONLY: Only staff/owners
        if address_visibility == ResourceVisibility.STAFF_ONLY:
            return self.is_org_staff(user_id)

        # Staff and owners can see everything (for all other visibility levels)
        if self.is_org_staff(user_id):
            return True

        # MEMBERS_ONLY: Organization members
        if address_visibility == ResourceVisibility.MEMBERS_ONLY:
            return self.is_org_member(user_id)

        # Check event relationships for PRIVATE and ATTENDEES_ONLY
        has_ticket = self.has_active_ticket(user_id)
        has_rsvp = self.has_active_rsvp(user_id)

        # ATTENDEES_ONLY: Only ticket holders or RSVPs (confirmed attendance)
        if address_visibility == ResourceVisibility.ATTENDEES_ONLY:
            return has_ticket or has_rsvp

        # PRIVATE: Invited users, ticket holders, or RSVPs
        if address_visibility == ResourceVisibility.PRIVATE:
            return has_ticket or has_rsvp or self.has_event_invitation(user_id)

        return False


# Mapping from notification types to required staff permissions.
# Only notification types that require specific permissions are listed here.
# If a notification type is not in this mapping, all staff/owners receive it.
NOTIFICATION_REQUIRED_PERMISSIONS: dict[NotificationType, str] = {
    NotificationType.QUESTIONNAIRE_SUBMITTED: "evaluate_questionnaire",
    NotificationType.TICKET_CREATED: "manage_tickets",
    NotificationType.TICKET_CANCELLED: "manage_tickets",
    NotificationType.TICKET_REFUNDED: "manage_tickets",
    NotificationType.INVITATION_REQUEST_CREATED: "invite_to_event",
    NotificationType.MEMBERSHIP_REQUEST_CREATED: "manage_members",
    NotificationType.WHITELIST_REQUEST_CREATED: "manage_members",
}


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
    batch_checker: BatchParticipationChecker | None = None,
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
        batch_checker: Optional pre-populated BatchParticipationChecker for O(1) lookups.
                       When provided, uses set lookups instead of per-user database queries.

    Returns:
        True if user should receive the notification
    """
    # Get user's notification preferences - use prefetched data when available
    # to avoid N+1 queries when called in a loop with select_related("notification_preferences")
    try:
        prefs = user.notification_preferences
    except NotificationPreference.DoesNotExist:
        # Create preferences if they don't exist (shouldn't happen normally,
        # as they're created on user creation via signals)
        prefs = NotificationPreference.objects.create(user=user)

    # Check global silence
    if prefs.silence_all_notifications:
        return False

    # Check if notification type is enabled
    if not prefs.is_notification_type_enabled(notification_type):
        return False

    # Check participation - use batch checker if available for O(1) lookups
    if event:
        if batch_checker is not None:
            # Use batch checker for O(1) set lookups (no database queries)
            if notification_type == NotificationType.EVENT_OPEN:
                return batch_checker.is_org_member(user.id) or batch_checker.is_participating(user.id)
            return batch_checker.is_participating(user.id)
        else:
            # Fallback to per-user queries (for single-user checks)
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
        RevelUser.objects.filter(participants_with_prefs_q).select_related("notification_preferences").distinct()
    )

    # Create batch checker to avoid N+1 queries when checking participation
    # This prefetches all RSVPs, tickets, invitations, and staff/member data in ~5 queries
    # instead of 4+ queries per user
    batch_checker = BatchParticipationChecker(event)

    # Filter based on notification preferences and participation rules
    # Using batch_checker for O(1) participation lookups
    eligible_user_ids: list[UUID] = []

    for user in potential_users:
        if is_user_eligible_for_notification(user, notification_type, event=event, batch_checker=batch_checker):
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


def get_organization_staff_with_permission(
    organization_id: UUID,
    permission: str,
) -> QuerySet[RevelUser]:
    """Get staff members and owners with a specific permission for an organization.

    Owners always have all permissions. Staff members are filtered by their
    permission settings stored in the JSON permissions field.

    Args:
        organization_id: The organization ID
        permission: The permission string to check (e.g., "evaluate_questionnaire")

    Returns:
        QuerySet of users with the specified permission, with notification preferences prefetched
    """
    # Owners always have all permissions
    owners_q = Q(owned_organizations=organization_id)

    # Staff with specific permission enabled in their default permissions
    # Uses JSONField lookup to check permissions.default.{permission} = true
    staff_with_permission_q = Q(
        organization_staff_memberships__organization_id=organization_id,
        **{f"organization_staff_memberships__permissions__default__{permission}": True},
    )

    return (
        RevelUser.objects.filter(owners_q | staff_with_permission_q)
        .select_related("notification_preferences")
        .distinct()
    )


def get_staff_for_notification(
    organization_id: UUID,
    notification_type: NotificationType,
) -> QuerySet[RevelUser]:
    """Get staff members eligible to receive a specific notification type.

    Uses the NOTIFICATION_REQUIRED_PERMISSIONS mapping to determine which
    permission is required. If no specific permission is required, returns
    all staff and owners.

    Args:
        organization_id: The organization ID
        notification_type: The type of notification being sent

    Returns:
        QuerySet of eligible users with notification preferences prefetched
    """
    required_permission = NOTIFICATION_REQUIRED_PERMISSIONS.get(notification_type)

    if required_permission:
        return get_organization_staff_with_permission(organization_id, required_permission)

    return get_organization_staff_and_owners(organization_id)


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
    log_kwargs = {
        "notification_type": notification_type.value,
        "user_email": user.email,
        "success": success,
    }

    if event:
        log_kwargs["event_name"] = event.name
        log_kwargs["event_id"] = str(event.id)

    if error_message:
        log_kwargs["error"] = error_message

    if success:
        logger.info("notification_sent", **log_kwargs)
    else:
        logger.error("notification_failed", **log_kwargs)
