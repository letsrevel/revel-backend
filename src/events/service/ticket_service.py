from __future__ import annotations

import typing as t
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from decimal import Decimal
from uuid import UUID

from django.db import transaction
from django.db.models import Count, F
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, EventInvitation, EventRSVP, MembershipTier, OrganizationMember, Ticket, TicketTier
from events.models.mixins import VisibilityMixin
from events.service import stripe_service

if t.TYPE_CHECKING:
    from events.service.event_manager import EventUserEligibility


@dataclass
class TierRemainingTickets:
    """Remaining tickets for a specific tier.

    Attributes:
        tier_id: The tier's UUID.
        remaining: How many more tickets the user can purchase (None = unlimited).
            This is based on user's personal limit and event capacity.
        sold_out: Whether the tier itself is sold out (no inventory remaining).
            This is independent of remaining - a user might have personal quota
            but the tier could still be sold out.
    """

    tier_id: UUID
    remaining: int | None  # None = unlimited
    sold_out: bool = False


@dataclass
class UserEventStatus:
    """User's status for an event including tickets, RSVP, and purchase limits."""

    tickets: list[Ticket]
    rsvp: EventRSVP | None = None
    can_purchase_more: bool = True
    remaining_tickets: list[TierRemainingTickets] = dataclass_field(default_factory=list)


def get_eligible_tiers(event: Event, user: RevelUser) -> list[TicketTier]:
    """Get ticket tiers the user is eligible to purchase from.

    A tier is eligible if ALL of these pass:
    1. Visibility check: user can see the tier (PUBLIC, MEMBERS_ONLY for members,
       PRIVATE for invited users, or user is org staff/owner)
    2. Sales window: currently within the tier's sales window
    3. Purchasable_by check: user is allowed to purchase (PUBLIC, MEMBERS for members,
       INVITED for invitees, INVITED_AND_MEMBERS for either)
    4. Membership tier restriction: user has the required membership tier (if restricted)

    Note:
        This function differs from EventManager.check_eligibility() in purpose:
        - check_eligibility() determines if user can ACCESS the event at all (blacklist,
          event status, invitation requirements, questionnaires, etc.)
        - get_eligible_tiers() determines which TIERS user can purchase from, assuming
          they already have event access.

        The caller should ensure event.organization is prefetched to avoid an extra query.

    Args:
        event: The event to check tiers for. Should have organization prefetched.
        user: The user to check eligibility for.

    Returns:
        List of TicketTier objects the user is eligible to purchase.
    """
    now = timezone.now()
    org = event.organization

    # Pre-fetch user context (single queries, not in loop)
    is_org_owner = org.owner_id == user.id
    is_org_staff = org.staff_members.filter(id=user.id).exists()
    is_staff_or_owner = is_org_owner or is_org_staff

    # Get user's active membership for this organization
    user_membership = OrganizationMember.objects.filter(
        organization=org,
        user=user,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    ).first()
    is_member = user_membership is not None
    user_membership_tier_ids = {user_membership.tier_id} if user_membership and user_membership.tier_id else set()

    # Check if user is invited to this event
    invitation = EventInvitation.objects.filter(event=event, user=user).first()
    is_invited = invitation is not None

    eligible: list[TicketTier] = []

    # Prefetch restricted_to_membership_tiers to avoid N+1 queries
    for tier in event.ticket_tiers.prefetch_related("restricted_to_membership_tiers").all():
        # 1. Check visibility
        if not _check_tier_visibility(tier, is_staff_or_owner, is_member, is_invited):
            continue

        # 2. Check sales window
        if tier.sales_start_at and now < tier.sales_start_at:
            continue
        if tier.sales_end_at and now > tier.sales_end_at:
            continue

        # 3. Check purchasable_by (who is allowed to purchase)
        if not _check_purchasable_by(tier, is_member, is_invited):
            continue

        # 4. Check membership tier restriction
        required_tier_ids = {mt.id for mt in tier.restricted_to_membership_tiers.all()}
        if required_tier_ids and not (user_membership_tier_ids & required_tier_ids):
            continue

        eligible.append(tier)

    return eligible


def _check_tier_visibility(
    tier: TicketTier,
    is_staff_or_owner: bool,
    is_member: bool,
    is_invited: bool,
) -> bool:
    """Check if user can see the tier based on visibility settings.

    Args:
        tier: The ticket tier to check.
        is_staff_or_owner: Whether user is org staff or owner.
        is_member: Whether user is an active org member.
        is_invited: Whether user has an invitation to the event.

    Returns:
        True if user can see the tier.
    """
    # Staff/owners can see all tiers
    if is_staff_or_owner:
        return True

    visibility = tier.visibility

    if visibility == VisibilityMixin.Visibility.PUBLIC:
        return True

    if visibility == VisibilityMixin.Visibility.MEMBERS_ONLY:
        return is_member

    if visibility == VisibilityMixin.Visibility.PRIVATE:
        return is_invited

    # STAFF_ONLY - only staff/owner (already checked above)
    return False


def _check_purchasable_by(
    tier: TicketTier,
    is_member: bool,
    is_invited: bool,
) -> bool:
    """Check if user is allowed to purchase from this tier based on purchasable_by setting.

    Note: Staff/owners are NOT exempt from purchasable_by restrictions. This is intentional:
    while they can SEE all tiers (visibility), they must still meet purchase requirements.
    A tier restricted to members-only requires staff to also be members to purchase.

    Args:
        tier: The ticket tier to check.
        is_member: Whether user is an active org member.
        is_invited: Whether user has an invitation to the event.

    Returns:
        True if user can purchase from this tier.
    """
    purchasable_by = tier.purchasable_by

    if purchasable_by == TicketTier.PurchasableBy.PUBLIC:
        return True

    if purchasable_by == TicketTier.PurchasableBy.MEMBERS:
        return is_member

    if purchasable_by == TicketTier.PurchasableBy.INVITED:
        return is_invited

    if purchasable_by == TicketTier.PurchasableBy.INVITED_AND_MEMBERS:
        return is_member or is_invited

    return False


def get_user_event_status(event: Event, user: RevelUser) -> UserEventStatus | EventUserEligibility:
    """Get user's current status for an event.

    Returns tickets, RSVP, and purchase limits for users who have already
    interacted with the event. For users with no interaction, returns
    eligibility information.

    Args:
        event: The event to check status for.
        user: The user to check.

    Returns:
        UserEventStatus if user has tickets or RSVP, otherwise EventUserEligibility.
    """
    from events.service.batch_ticket_service import BatchTicketService
    from events.service.event_manager import EventManager

    # Get all user's tickets for this event using the optimized full() queryset
    tickets = list(Ticket.objects.full().filter(event=event, user_id=user.id).order_by("-created_at"))

    if tickets and event.requires_ticket:
        # Calculate event-level capacity remaining (once, to avoid N+1)
        # Uses effective_capacity (min of max_attendees and venue.capacity)
        event_capacity_remaining: int | None = None
        effective_cap = event.effective_capacity
        if effective_cap > 0:
            total_sold = Ticket.objects.filter(event=event).exclude(status=Ticket.TicketStatus.CANCELLED).count()
            event_capacity_remaining = max(0, effective_cap - total_sold)

        # Pre-compute user's ticket counts per tier in ONE query (avoids N+1)
        user_ticket_counts: dict[UUID, int] = dict(
            Ticket.objects.filter(
                event=event,
                user=user,
                status__in=[Ticket.TicketStatus.PENDING, Ticket.TicketStatus.ACTIVE],
            )
            .values("tier_id")
            .annotate(count=Count("id"))
            .values_list("tier_id", "count")
        )

        # Get all eligible tiers for this user and calculate remaining for each
        eligible_tiers = get_eligible_tiers(event, user)
        remaining_list: list[TierRemainingTickets] = []

        for tier in eligible_tiers:
            service = BatchTicketService(event, tier, user)
            tier_count = user_ticket_counts.get(tier.id, 0)
            remaining = service.get_remaining_tickets(event_capacity_remaining, user_ticket_count=tier_count)

            # Check if tier is sold out (total_quantity - quantity_sold <= 0)
            tier_sold_out = False
            if tier.total_quantity is not None:
                tier_sold_out = (tier.total_quantity - tier.quantity_sold) <= 0

            remaining_list.append(TierRemainingTickets(tier_id=tier.id, remaining=remaining, sold_out=tier_sold_out))

        # can_purchase_more is True if any tier has remaining quota AND is not sold out
        can_purchase = any((r.remaining is None or r.remaining > 0) and not r.sold_out for r in remaining_list)

        return UserEventStatus(
            tickets=tickets,
            can_purchase_more=can_purchase,
            remaining_tickets=remaining_list,
        )

    # Check for RSVP (non-ticketed events)
    rsvp = EventRSVP.objects.filter(event=event, user_id=user.id).first()
    if rsvp:
        return UserEventStatus(tickets=[], rsvp=rsvp)

    # No tickets or RSVP - return eligibility check
    return EventManager(user, event).check_eligibility()


class TicketService:
    def __init__(self, *, event: Event, tier: TicketTier, user: RevelUser) -> None:
        """Initialize the ticket service."""
        self.event = event
        self.tier = tier
        self.user = user

    def checkout(self, price_override: Decimal | None = None) -> str | Ticket:
        """Conditional checkout."""
        if (
            Ticket.objects.filter(event=self.event, tier=self.tier, user=self.user).exists()
            and self.tier.payment_method != TicketTier.PaymentMethod.ONLINE
        ):
            raise HttpError(400, str(_("You already have a ticket.")))
        match self.tier.payment_method:
            case TicketTier.PaymentMethod.ONLINE:
                return self._stripe_checkout(price_override=price_override)
            case TicketTier.PaymentMethod.OFFLINE:
                return self._offline_checkout()
            case TicketTier.PaymentMethod.AT_THE_DOOR:
                return self._at_the_door_checkout()
            case TicketTier.PaymentMethod.FREE:
                return self._free_checkout()
            case _:
                raise HttpError(400, str(_("Unknown payment method.")))

    def _stripe_checkout(self, price_override: Decimal | None = None) -> str:
        checkout_url, _ = stripe_service.create_checkout_session(
            self.event, self.tier, self.user, price_override=price_override
        )
        return checkout_url

    @transaction.atomic
    def _offline_checkout(self) -> Ticket:
        TicketTier.objects.select_for_update().filter(pk=self.tier.pk).update(quantity_sold=F("quantity_sold") + 1)
        ticket = Ticket.objects.create(
            event=self.event,
            tier=self.tier,
            user=self.user,
            status=Ticket.TicketStatus.PENDING,
            guest_name=self.user.get_display_name(),
        )
        # Notification sent automatically via post_save signal
        return ticket

    def _at_the_door_checkout(self) -> Ticket:
        """Handle at-the-door checkout - creates ACTIVE tickets immediately.

        AT_THE_DOOR represents a commitment to attend (pay at arrival),
        so tickets are marked ACTIVE and count toward attendee_count.
        """
        return self._free_checkout()

    @transaction.atomic
    def _free_checkout(self) -> Ticket:
        TicketTier.objects.select_for_update().filter(pk=self.tier.pk).update(quantity_sold=F("quantity_sold") + 1)
        ticket = Ticket.objects.create(
            event=self.event,
            tier=self.tier,
            user=self.user,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name=self.user.get_display_name(),
        )
        # Notification sent automatically via post_save signal
        return ticket


def check_in_ticket(event: Event, ticket_id: UUID, checked_in_by: RevelUser) -> Ticket:
    """Check in an attendee by scanning their ticket."""
    # Get the ticket
    ticket = get_object_or_404(
        Ticket.objects.select_related("user", "tier"),
        pk=ticket_id,
        event=event,
    )

    # Check if ticket status is valid for check-in
    # ACTIVE tickets can be checked in directly.
    # PENDING tickets are only allowed for OFFLINE payment method (payment will be collected at check-in).
    # AT_THE_DOOR tickets are now created as ACTIVE, so no special handling needed.
    if ticket.status != Ticket.TicketStatus.ACTIVE:
        if not (
            ticket.status == Ticket.TicketStatus.PENDING
            and ticket.tier.payment_method == TicketTier.PaymentMethod.OFFLINE
        ):
            # Determine appropriate error message based on ticket status
            if ticket.status == Ticket.TicketStatus.CHECKED_IN:
                error_message = str(_("This ticket has already been checked in."))
            elif ticket.status == Ticket.TicketStatus.CANCELLED:
                error_message = str(_("This ticket has been cancelled."))
            elif ticket.status == Ticket.TicketStatus.PENDING:
                error_message = str(_("This ticket is pending payment confirmation."))
            else:
                error_message = str(_("Invalid ticket status: {status}")).format(status=ticket.status)
            raise HttpError(400, error_message)

    # Check if check-in window is open
    if not event.is_check_in_open():
        raise HttpError(400, str(_("Check-in is not currently open for this event.")))

    # Update ticket status
    ticket.status = Ticket.TicketStatus.CHECKED_IN
    ticket.checked_in_at = timezone.now()
    ticket.checked_in_by = checked_in_by
    ticket.save(update_fields=["status", "checked_in_at", "checked_in_by"])

    return ticket


@transaction.atomic
def create_ticket_tier(
    event: Event, tier_data: dict[str, t.Any], restricted_to_membership_tiers_ids: list[UUID] | None = None
) -> TicketTier:
    """Create a ticket tier with membership tier restrictions.

    Args:
        event: The event for this ticket tier
        tier_data: Dictionary of TicketTier model fields
        restricted_to_membership_tiers_ids: Optional list of MembershipTier IDs to restrict this tier to

    Returns:
        Created TicketTier instance

    Raises:
        Http404: If any membership tier ID doesn't exist or doesn't belong to event's organization

    Note:
        TimeStampedModel.save() automatically calls full_clean() before saving.
        After setting M2M relationships, we call full_clean() again to validate them.
    """
    # Create the ticket tier (save() will call full_clean() automatically)
    tier = TicketTier.objects.create(event=event, **tier_data)

    # Handle membership tier restrictions
    if restricted_to_membership_tiers_ids:
        # Fetch and validate membership tiers
        membership_tiers = MembershipTier.objects.filter(
            id__in=restricted_to_membership_tiers_ids, organization=event.organization
        )

        # Ensure all provided IDs exist and belong to the organization
        if membership_tiers.count() != len(restricted_to_membership_tiers_ids):
            # Transaction will rollback automatically due to exception
            raise HttpError(
                404,
                str(_("One or more membership tier IDs are invalid or don't belong to the event's organization.")),
            )

        # Set the M2M relationship
        tier.restricted_to_membership_tiers.set(membership_tiers)

        # Validate M2M relationships (TicketTier.clean() checks membership tiers)
        tier.full_clean()

    return tier


@transaction.atomic
def update_ticket_tier(
    tier: TicketTier, tier_data: dict[str, t.Any], restricted_to_membership_tiers_ids: list[UUID] | None = None
) -> TicketTier:
    """Update a ticket tier with membership tier restrictions.

    Args:
        tier: The TicketTier instance to update
        tier_data: Dictionary of fields to update
        restricted_to_membership_tiers_ids: Optional list of MembershipTier IDs (replaces existing)
            - If list provided: replaces all restrictions with new list
            - If empty list provided: clears all restrictions
            - If None (not provided): preserves existing restrictions

    Returns:
        Updated TicketTier instance

    Raises:
        Http404: If any membership tier ID doesn't exist or doesn't belong to event's organization

    Note:
        TimeStampedModel.save() automatically calls full_clean() before saving.
        After updating M2M relationships, we call full_clean() again to validate them.
    """
    # Update regular fields
    for field, value in tier_data.items():
        setattr(tier, field, value)

    if tier_data:
        # save() will call full_clean() automatically via TimeStampedModel
        tier.save(update_fields=list(tier_data.keys()))

    # Handle membership tier restrictions update
    if restricted_to_membership_tiers_ids is not None:
        if restricted_to_membership_tiers_ids:
            # Fetch and validate membership tiers
            membership_tiers = MembershipTier.objects.filter(
                id__in=restricted_to_membership_tiers_ids, organization=tier.event.organization
            )

            # Ensure all provided IDs exist and belong to the organization
            if membership_tiers.count() != len(restricted_to_membership_tiers_ids):
                raise HttpError(
                    404,
                    str(_("One or more membership tier IDs are invalid or don't belong to the event's organization.")),
                )

            # Replace the M2M relationship
            tier.restricted_to_membership_tiers.set(membership_tiers)
        else:
            # Empty list means clear all restrictions
            tier.restricted_to_membership_tiers.clear()

        # Validate M2M relationships (TicketTier.clean() checks membership tiers and purchasable_by logic)
        tier.full_clean()

    return tier
