from __future__ import annotations

import typing as t
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Count, F, Max
from django.shortcuts import get_object_or_404
from django.utils import formats, timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.exceptions import (
    BillingInfoRequiredError,
    StripeNotConnectedError,
    TicketAlreadyCancelledError,
)
from events.models import (
    Event,
    EventInvitation,
    EventRSVP,
    MembershipTier,
    Organization,
    OrganizationMember,
    Payment,
    Ticket,
    TicketTier,
)
from events.models.mixins import VisibilityMixin
from events.models.ticket import CancellationSource
from events.service.waitlist_service import enqueue_waitlist_processing

if t.TYPE_CHECKING:
    from common.models import FileExport
    from events.schema import TicketTierCreateSchema, TicketTierUpdateSchema
    from events.service.event_manager import EventUserEligibility


# Translated messages exposed for HTTP mapping at the controller layer. Keeping
# them next to the typed exceptions lets the service own the canonical wording
# while the controller only maps the exception to a status code.
TICKET_ALREADY_CANCELLED_MESSAGE = _("Ticket already cancelled")
STRIPE_NOT_CONNECTED_MESSAGE = _("You must connect to Stripe first.")
BILLING_INFO_REQUIRED_MESSAGE = _(
    "Billing information is required for online ticket sales with platform fees."
    " Please set your billing name, country and billing address"
    " in your organization's billing settings."
)


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
    can_purchase: bool = True


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
    is_staff_or_owner = org.is_owner_or_staff(user)

    # Get user's active membership for this organization
    user_membership = OrganizationMember.objects.filter(
        organization=org,
        user=user,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    ).first()
    is_member = user_membership is not None
    user_membership_tier_ids = {user_membership.tier_id} if user_membership and user_membership.tier_id else set()

    # Check if user is invited to this event and get linked tier IDs
    invitation = EventInvitation.objects.prefetch_related("tiers").filter(event=event, user=user).first()
    is_invited = invitation is not None
    invitation_tier_ids = set(invitation.tiers.values_list("id", flat=True)) if invitation else set()

    eligible: list[TicketTier] = []

    # Prefetch restricted_to_membership_tiers to avoid N+1 queries
    for tier in event.ticket_tiers.prefetch_related("restricted_to_membership_tiers").all():
        # 1. Check visibility
        if not _check_tier_visibility(tier, is_staff_or_owner, is_member, is_invited, invitation_tier_ids):
            continue

        # 2. Check sales window
        if tier.sales_start_at and now < tier.sales_start_at:
            continue
        if tier.sales_end_at and now > tier.sales_end_at:
            continue

        # 3. Check purchasable_by (staff/owners are exempt, consistent with _assert_purchasable_by)
        if not is_staff_or_owner:
            if not _check_purchasable_by(tier, is_member, is_invited, invitation_tier_ids):
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
    invitation_tier_ids: set[UUID],
) -> bool:
    """Check if user can see the tier based on visibility settings.

    Args:
        tier: The ticket tier to check.
        is_staff_or_owner: Whether user is org staff or owner.
        is_member: Whether user is an active org member.
        is_invited: Whether user has an invitation to the event.
        invitation_tier_ids: Set of tier IDs linked to user's invitation.

    Returns:
        True if user can see the tier.
    """
    # Staff/owners can see all tiers
    if is_staff_or_owner:
        return True

    visibility = tier.visibility

    if visibility in VisibilityMixin.Visibility.publicly_accessible():
        return True

    if visibility == VisibilityMixin.Visibility.MEMBERS_ONLY:
        return is_member

    if visibility == VisibilityMixin.Visibility.PRIVATE:
        if tier.restrict_visibility_to_linked_invitations:
            return tier.id in invitation_tier_ids
        return is_invited

    # STAFF_ONLY - only staff/owner (already checked above)
    return False


def _check_purchasable_by(
    tier: TicketTier,
    is_member: bool,
    is_invited: bool,
    invitation_tier_ids: set[UUID],
) -> bool:
    """Check if a regular user is allowed to purchase from this tier based on purchasable_by setting.

    Note: Staff/owners are exempted by the caller (get_eligible_tiers and _assert_purchasable_by)
    before this function is reached. This function only evaluates membership/invitation rules.

    Args:
        tier: The ticket tier to check.
        is_member: Whether user is an active org member.
        is_invited: Whether user has an invitation to the event.
        invitation_tier_ids: Set of tier IDs linked to user's invitation.

    Returns:
        True if user can purchase from this tier.
    """
    purchasable_by = tier.purchasable_by

    if purchasable_by == TicketTier.PurchasableBy.PUBLIC:
        return True

    if purchasable_by == TicketTier.PurchasableBy.MEMBERS:
        return is_member

    if purchasable_by == TicketTier.PurchasableBy.INVITED:
        if tier.restrict_purchase_to_linked_invitations:
            return tier.id in invitation_tier_ids
        return is_invited

    if purchasable_by == TicketTier.PurchasableBy.INVITED_AND_MEMBERS:
        invited_ok = is_invited
        if tier.restrict_purchase_to_linked_invitations:
            invited_ok = tier.id in invitation_tier_ids
        return is_member or invited_ok

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

    has_active_tickets = any(t.status != Ticket.TicketStatus.CANCELLED for t in tickets)

    if not has_active_tickets or not event.requires_ticket:
        # Check for RSVP (non-ticketed events)
        if rsvp := EventRSVP.objects.filter(event=event, user_id=user.id).first():
            return UserEventStatus(tickets=[], rsvp=rsvp)
        # No active tickets or RSVP - run eligibility check
        eligibility = EventManager(user, event).check_eligibility()
        if not eligibility.allowed or not tickets:
            return eligibility
        # User has only cancelled tickets but is eligible - fall through to show purchase capacity

    # Calculate event-level capacity remaining (once, to avoid N+1)
    # Uses effective_capacity (min of max_attendees and venue.capacity)
    event_capacity_remaining: int | None = None
    if (effective_cap := event.effective_capacity) > 0:
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

    # Get eligible tiers (can purchase) and all visible tiers for this user
    eligible_tiers = get_eligible_tiers(event, user)
    eligible_tier_ids = {t.id for t in eligible_tiers}
    visible_tiers = list(TicketTier.objects.for_visible_event(event, user))

    remaining_list: list[TierRemainingTickets] = []

    for tier in visible_tiers:
        is_eligible = tier.id in eligible_tier_ids
        if is_eligible:
            service = BatchTicketService(event, tier, user)
            tier_count = user_ticket_counts.get(tier.id, 0)
            remaining = service.get_remaining_tickets(event_capacity_remaining, user_ticket_count=tier_count)
            tier_sold_out = tier.total_quantity is not None and (tier.total_quantity - tier.quantity_sold) <= 0
            remaining_list.append(
                TierRemainingTickets(tier_id=tier.id, remaining=remaining, sold_out=tier_sold_out, can_purchase=True)
            )
        else:
            remaining_list.append(
                TierRemainingTickets(tier_id=tier.id, remaining=None, sold_out=False, can_purchase=False)
            )

    # can_purchase_more is True if any eligible tier has remaining quota AND is not sold out
    can_purchase = any(
        r.can_purchase and (r.remaining is None or r.remaining > 0) and not r.sold_out for r in remaining_list
    )

    return UserEventStatus(
        tickets=tickets,
        can_purchase_more=can_purchase,
        remaining_tickets=remaining_list,
    )


@transaction.atomic
def confirm_ticket_payment(ticket: Ticket, price_paid: Decimal | None = None) -> Ticket:
    """Confirm payment for a pending offline/at-the-door ticket and activate it.

    Args:
        ticket: The ticket to confirm. Must have tier prefetched via select_related.
        price_paid: Amount paid. Required for PWYC tiers that don't already have a
            recorded price. Optional as an override for PWYC tiers that already have
            a price (e.g. set during batch checkout). Forbidden for fixed-price tiers.

    Returns:
        The re-fetched ticket with full() relations for serialization.

    Note:
        price_paid is intentionally not validated against the tier's pwyc_min/pwyc_max
        bounds. Admins are trusted to override these limits when confirming payment
        (e.g. accepting a lower amount or granting a discount).

    Raises:
        HttpError 400: If price_paid is missing for PWYC without existing price,
            or provided for fixed-price.
    """
    is_pwyc = ticket.tier.price_type == TicketTier.PriceType.PWYC

    if not is_pwyc and price_paid is not None:
        raise HttpError(400, str(_("Price paid is not allowed for fixed-price tiers.")))

    if is_pwyc and price_paid is None and ticket.price_paid is None:
        raise HttpError(400, str(_("Price paid is required for Pay What You Can tiers.")))

    update_fields = ["status"]

    ticket.status = Ticket.TicketStatus.ACTIVE

    if is_pwyc and price_paid is not None:
        ticket.price_paid = price_paid
        update_fields.append("price_paid")

    ticket.save(update_fields=update_fields)

    # Re-fetch with full() to include all related objects for serialization
    return Ticket.objects.full().get(pk=ticket.pk)


@transaction.atomic
def unconfirm_ticket_payment(ticket: Ticket) -> Ticket:
    """Revert a confirmed offline ticket back to pending status, clearing price_paid.

    Args:
        ticket: The ticket to unconfirm. Must be ACTIVE with OFFLINE payment method.

    Returns:
        The re-fetched ticket with full() relations for serialization.
    """
    ticket.status = Ticket.TicketStatus.PENDING
    ticket.price_paid = None
    ticket.save(update_fields=["status", "price_paid"])

    # Re-fetch with full() to include all related objects for serialization
    return Ticket.objects.full().get(pk=ticket.pk)


def _format_in_event_tz(dt: datetime, event: Event) -> str:
    """Format ``dt`` in the event's local timezone (via its city), falling back to Django's active timezone.

    The tz abbreviation (``CET``, ``UTC``, …) is appended so the user can't misread an ambiguous local time.
    """
    tz: ZoneInfo | t.Any
    if event.city and event.city.timezone:
        try:
            tz = ZoneInfo(event.city.timezone)
        except KeyError:
            tz = timezone.get_current_timezone()
    else:
        tz = timezone.get_current_timezone()
    local = dt.astimezone(tz)
    return f"{formats.date_format(local, 'DATETIME_FORMAT', use_l10n=True)} {local.tzname() or ''}".rstrip()


def _check_in_closed_message(event: Event) -> str:
    """Build a localized error message for a closed check-in window, surfacing the open/close time when known."""
    if event.status != event.EventStatus.OPEN:
        return str(_("Check-in is not currently open for this event."))
    now = timezone.now()
    starts_at = event.check_in_starts_at or event.start
    ends_at = event.check_in_ends_at or event.end
    if now < starts_at:
        return str(_("Check-in is not open yet. It will open at {opens_at}.")).format(
            opens_at=_format_in_event_tz(starts_at, event)
        )
    if now > ends_at:
        return str(_("Check-in has closed for this event. It ended at {ended_at}.")).format(
            ended_at=_format_in_event_tz(ends_at, event)
        )
    return str(_("Check-in is not currently open for this event."))


def check_in_ticket(
    event: Event, ticket_id: UUID, checked_in_by: RevelUser, price_paid: Decimal | None = None
) -> Ticket:
    """Check in an attendee by scanning their ticket.

    Args:
        event: The event the ticket belongs to.
        ticket_id: UUID of the ticket to check in.
        checked_in_by: The user performing the check-in.
        price_paid: Amount paid. Required for PWYC offline/at-the-door tickets
            that don't have a price recorded yet. Optional as an override for
            PWYC offline/at-the-door tickets that already have a price.
            Forbidden for non-PWYC or online tickets.

    Note:
        price_paid is intentionally not validated against the tier's pwyc_min/pwyc_max
        bounds. Admins are trusted to override these limits at check-in.
    """
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
        raise HttpError(400, _check_in_closed_message(event))

    # PWYC price_paid handling
    is_pwyc_offsite = ticket.tier.price_type == TicketTier.PriceType.PWYC and ticket.tier.payment_method in (
        TicketTier.PaymentMethod.OFFLINE,
        TicketTier.PaymentMethod.AT_THE_DOOR,
    )

    if not is_pwyc_offsite and price_paid is not None:
        raise HttpError(400, str(_("Price paid is not allowed for this ticket.")))

    if is_pwyc_offsite and price_paid is None and ticket.price_paid is None:
        raise HttpError(400, str(_("Price paid is required for Pay What You Can tickets without a recorded payment.")))

    # Update ticket status
    update_fields = ["status", "checked_in_at", "checked_in_by"]
    if is_pwyc_offsite and price_paid is not None:
        ticket.price_paid = price_paid
        update_fields.append("price_paid")

    ticket.status = Ticket.TicketStatus.CHECKED_IN
    ticket.checked_in_at = timezone.now()
    ticket.checked_in_by = checked_in_by
    ticket.save(update_fields=update_fields)

    return ticket


@transaction.atomic
def create_ticket_tier(event: Event, payload: "TicketTierCreateSchema") -> TicketTier:
    """Create a ticket tier from a typed payload, validating online prerequisites and M2M restrictions.

    Args:
        event: The event for this ticket tier.
        payload: The validated ``TicketTierCreateSchema`` payload.

    Returns:
        The newly-created tier, re-fetched via ``with_venue_and_sector()`` for serialization.

    Raises:
        StripeNotConnectedError: When the tier is online-payment but the org has no Stripe Connect.
        BillingInfoRequiredError: When the tier is online-payment with platform fees and the
            org has incomplete billing info.
        HttpError 404: If any provided membership tier ID is invalid or belongs to another org.

    Note:
        ``mode="json"`` is used when dumping the payload so nested Pydantic models
        (e.g. ``refund_policy``) and ``Decimal`` are coerced to JSON-serializable primitives;
        the JSONField's default encoder relies on this during ``full_clean()``.
    """
    check_online_tier_prerequisites(event.organization, payload.payment_method)

    payload_dict = payload.model_dump(exclude_unset=True, mode="json")
    restricted_to_membership_tiers_ids = payload_dict.pop("restricted_to_membership_tiers_ids", None)

    # Append new tiers at the bottom of the list unless the caller pinned an explicit
    # position. Model ordering is ["event", "display_order", "name"], so leaving the
    # field at its default 0 would sort every new tier to the top (see #514).
    if "display_order" not in payload_dict:
        current_max = TicketTier.objects.filter(event=event).aggregate(m=Max("display_order"))["m"]
        payload_dict["display_order"] = 0 if current_max is None else current_max + 1

    # Create the ticket tier (save() will call full_clean() automatically)
    tier = TicketTier.objects.create(event=event, **payload_dict)

    if restricted_to_membership_tiers_ids:
        _set_tier_membership_restrictions(tier, restricted_to_membership_tiers_ids, event.organization)

    return TicketTier.objects.with_venue_and_sector().get(pk=tier.pk)


@transaction.atomic
def reorder_ticket_tiers(event: Event, tier_ids: list[UUID]) -> None:
    """Reorder ticket tiers for an event by setting display_order from the list position.

    Args:
        event: The event whose tiers are being reordered.
        tier_ids: Ordered list of tier UUIDs representing the desired display order.

    Raises:
        HttpError 400: If tier_ids don't match the event's tiers exactly.
    """
    existing_ids = set(TicketTier.objects.filter(event=event).values_list("id", flat=True))

    if set(tier_ids) != existing_ids:
        raise HttpError(400, str(_("Tier IDs must match all tiers for this event exactly.")))

    tiers_to_update = []
    for index, tier_id in enumerate(tier_ids):
        tier = TicketTier(pk=tier_id, display_order=index)
        tiers_to_update.append(tier)

    TicketTier.objects.bulk_update(tiers_to_update, ["display_order"])


@transaction.atomic
def update_ticket_tier(tier: TicketTier, payload: "TicketTierUpdateSchema") -> TicketTier:
    """Update a ticket tier from a typed payload, validating online prerequisites and M2M restrictions.

    Args:
        tier: The ``TicketTier`` instance to update.
        payload: The validated ``TicketTierUpdateSchema`` payload.

    Returns:
        The updated tier, re-fetched via ``with_venue_and_sector()`` for serialization.

    Raises:
        StripeNotConnectedError: When transitioning to online payment but org has no Stripe Connect.
        BillingInfoRequiredError: When transitioning to online payment with platform fees and the
            org has incomplete billing info.
        HttpError 404: If any provided membership tier ID is invalid or belongs to another org.

    Note:
        ``restricted_to_membership_tiers_ids`` semantics:
            - non-empty list -> replace all restrictions
            - empty list     -> clear all restrictions
            - omitted (None) -> preserve existing restrictions

        ``mode="json"`` see ``create_ticket_tier`` above.
    """
    payload_dict = payload.model_dump(exclude_unset=True, mode="json")
    restricted_to_membership_tiers_ids = payload_dict.pop("restricted_to_membership_tiers_ids", None)

    if payload.payment_method is not None:
        check_online_tier_prerequisites(tier.event.organization, payload.payment_method)

    # Update regular fields
    for field, value in payload_dict.items():
        setattr(tier, field, value)

    if payload_dict:
        # save() will call full_clean() automatically via TimeStampedModel
        tier.save(update_fields=list(payload_dict.keys()))

    # Handle membership tier restrictions update
    if restricted_to_membership_tiers_ids is not None:
        if restricted_to_membership_tiers_ids:
            _set_tier_membership_restrictions(tier, restricted_to_membership_tiers_ids, tier.event.organization)
        else:
            # Empty list means clear all restrictions
            tier.restricted_to_membership_tiers.clear()
            # Validate M2M relationships (TicketTier.clean() checks membership tiers and purchasable_by logic)
            tier.full_clean()

    return TicketTier.objects.with_venue_and_sector().get(pk=tier.pk)


def _set_tier_membership_restrictions(
    tier: TicketTier, restricted_to_membership_tiers_ids: list[UUID], organization: "Organization"
) -> None:
    """Validate membership tier IDs against the organization, then set them on the tier.

    Raises:
        HttpError 404: If any ID is unknown or belongs to a different organization.
    """
    membership_tiers = MembershipTier.objects.filter(
        id__in=restricted_to_membership_tiers_ids, organization=organization
    )

    if membership_tiers.count() != len(restricted_to_membership_tiers_ids):
        raise HttpError(
            404,
            str(_("One or more membership tier IDs are invalid or don't belong to the event's organization.")),
        )

    tier.restricted_to_membership_tiers.set(membership_tiers)
    # Validate M2M relationships (TicketTier.clean() checks membership tiers and purchasable_by logic)
    tier.full_clean()


def check_online_tier_prerequisites(org: "Organization", payment_method: TicketTier.PaymentMethod) -> None:
    """Validate prerequisites for creating/updating an online-payment ticket tier.

    Args:
        org: The owning organization.
        payment_method: The tier's payment method.

    Raises:
        StripeNotConnectedError: If Stripe Connect is not enabled on the organization.
        BillingInfoRequiredError: If platform fees are non-zero but billing info is incomplete.
    """
    if payment_method != TicketTier.PaymentMethod.ONLINE:
        return

    if not org.is_stripe_connected:
        raise StripeNotConnectedError

    has_platform_fees = org.platform_fee_percent > 0 or org.platform_fee_fixed > 0
    missing_billing = not org.vat_country_code or not org.billing_address or not org.billing_name
    if has_platform_fees and missing_billing:
        raise BillingInfoRequiredError


def _cancel_offline_ticket_core(
    ticket: Ticket,
    *,
    cancelled_by: RevelUser,
    reason: str,
) -> None:
    """Apply the shared cancellation primitive: tier decrement + ticket cancel fields + waitlist enqueue.

    This mutates ``ticket`` in place (status, cancelled_at, cancelled_by, cancellation_source,
    cancellation_reason). It must be called inside a ``transaction.atomic()`` block by the caller
    (``cancel_offline_ticket`` / ``mark_offline_ticket_refunded``) with the ticket row already
    locked via ``select_for_update`` to prevent concurrent double-decrement.

    The tier decrement uses ``F("quantity_sold") - 1`` guarded by ``quantity_sold__gt=0`` so the
    counter can never drop below zero (race-safe and floor-safe).
    """
    TicketTier.objects.filter(pk=ticket.tier_id, quantity_sold__gt=0).update(quantity_sold=F("quantity_sold") - 1)

    ticket.status = Ticket.TicketStatus.CANCELLED
    ticket.cancelled_at = timezone.now()
    ticket.cancelled_by = cancelled_by
    ticket.cancellation_source = CancellationSource.ORGANIZER
    ticket.cancellation_reason = reason
    ticket.save(
        update_fields=[
            "status",
            "cancelled_at",
            "cancelled_by",
            "cancellation_source",
            "cancellation_reason",
        ]
    )

    enqueue_waitlist_processing(ticket.event_id)


@transaction.atomic
def cancel_offline_ticket(
    ticket: Ticket,
    *,
    cancelled_by: RevelUser,
    reason: str | None = None,
) -> Ticket:
    """Cancel an offline/at-the-door ticket and record organizer audit fields.

    The ticket row is re-fetched with ``select_for_update`` inside the atomic block
    so concurrent cancel/refund requests serialize on the row lock and cannot both
    pass the status check and double-decrement ``TicketTier.quantity_sold``.

    Args:
        ticket: The ticket to cancel.
        cancelled_by: The organizer performing the cancellation.
        reason: Optional free-text cancellation reason.

    Returns:
        The re-fetched ticket via ``full()`` for response serialization.

    Raises:
        TicketAlreadyCancelledError: If the ticket is already CANCELLED.
    """
    locked_ticket = Ticket.objects.select_for_update().select_related("tier").get(pk=ticket.pk)
    if locked_ticket.status == Ticket.TicketStatus.CANCELLED:
        raise TicketAlreadyCancelledError

    _cancel_offline_ticket_core(locked_ticket, cancelled_by=cancelled_by, reason=reason or "")

    return Ticket.objects.full().get(pk=locked_ticket.pk)


@transaction.atomic
def mark_offline_ticket_refunded(
    ticket: Ticket,
    *,
    cancelled_by: RevelUser,
    reason: str | None = None,
) -> Ticket:
    """Mark a manual offline/at-the-door ticket as refunded and cancel it.

    Layers a ``Payment`` refund mutation on top of the shared cancellation primitive.
    Tickets without an associated ``Payment`` are still cancelled — no payment record
    means there is nothing to refund, which is a valid manual flow.

    The ticket and payment rows are re-fetched with ``select_for_update`` inside the
    atomic block so concurrent cancel/refund requests serialize on the row locks and
    cannot both pass the status check and double-apply side effects (tier decrement,
    waitlist enqueue, payment refund).

    Args:
        ticket: The ticket to refund.
        cancelled_by: The organizer performing the refund.
        reason: Optional free-text cancellation reason.

    Returns:
        The re-fetched ticket via ``full()`` for response serialization.

    Raises:
        TicketAlreadyCancelledError: If the ticket is already CANCELLED.
    """
    locked_ticket = Ticket.objects.select_for_update().select_related("tier").get(pk=ticket.pk)
    if locked_ticket.status == Ticket.TicketStatus.CANCELLED:
        raise TicketAlreadyCancelledError

    _cancel_offline_ticket_core(locked_ticket, cancelled_by=cancelled_by, reason=reason or "")

    locked_payment = Payment.objects.select_for_update().filter(ticket=locked_ticket).first()
    if locked_payment is not None:
        locked_payment.status = Payment.PaymentStatus.REFUNDED
        locked_payment.refund_amount = locked_payment.amount
        locked_payment.refund_status = Payment.RefundStatus.SUCCEEDED
        locked_payment.refunded_at = timezone.now()
        locked_payment.save(update_fields=["status", "refund_amount", "refund_status", "refunded_at"])

    return Ticket.objects.full().get(pk=locked_ticket.pk)


def start_attendee_export(event: Event, requested_by: RevelUser) -> "FileExport":
    """Create a ``FileExport`` record for an attendee-list export and dispatch the export task.

    Args:
        event: The event whose attendee list should be exported.
        requested_by: The user requesting the export (recorded on the FileExport).

    Returns:
        The newly-created ``FileExport`` in PENDING state.
    """
    from common.models import FileExport
    from events.tasks import generate_attendee_export_task

    export = FileExport.objects.create(
        requested_by=requested_by,
        export_type=FileExport.ExportType.ATTENDEE_LIST,
        parameters={"event_id": str(event.id)},
    )
    # Defer dispatch until after the surrounding transaction commits so the
    # worker can SELECT the FileExport row. Consistent with the questionnaire
    # export pattern.
    transaction.on_commit(lambda: generate_attendee_export_task.delay(str(export.id)))
    return export
