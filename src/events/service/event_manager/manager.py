"""EventManager for handling RSVP and ticket operations."""

from decimal import Decimal

from django.db import transaction
from django.db.models import F
from django.utils.translation import gettext as _

from accounts.models import RevelUser
from events import models
from events.models import (
    EventRSVP,
    OrganizationMember,
    Ticket,
    TicketTier,
)
from events.service.ticket_service import TicketService

from .enums import NextStep, Reasons
from .service import EligibilityService
from .types import EventUserEligibility, UserIsIneligibleError


class EventManager:
    """The Event Manager Class.

    It is responsible to handle RSVP and ticket issuance for events,
    ensuring eligibility checks pass and there are no race conditions.
    """

    def __init__(self, user: RevelUser, event: models.Event) -> None:
        """Initialize the EventManager."""
        self.user = user
        self.event = event
        self.eligibility_service = EligibilityService(user, event)

    @transaction.atomic
    def rsvp(self, answer: EventRSVP.RsvpStatus, bypass_eligibility_checks: bool = False) -> EventRSVP:
        """RSVP to an event.

        A user can RSVP if an Event DOES not require a ticket, AND:
        - an event is private, and the user has an invitation for that event
        - an event is members only and the user is a member (or staff member)
        - an event is public

        Users who have already RSVP'd YES can always change their status to MAYBE or NO,
        even if eligibility requirements have changed since their initial RSVP.

        Returns:
            EventRSVP

        Raises:
            UserIsIneligibleError
        """
        if self.event.requires_ticket:
            raise UserIsIneligibleError(
                message="You must get a ticket for this event.",
                eligibility=EventUserEligibility(
                    allowed=False,
                    event_id=self.event.id,
                    next_step=NextStep.PURCHASE_TICKET,
                    reason=_(Reasons.REQUIRES_TICKET),
                ),
            )

        # Users who already RSVP'd YES can freely change to MAYBE/NO
        # This prevents them from being "trapped" if eligibility requirements change
        has_yes_rsvp = EventRSVP.objects.filter(
            user=self.user, event=self.event, status=EventRSVP.RsvpStatus.YES
        ).exists()
        if has_yes_rsvp:
            bypass_eligibility_checks = True

        eligibility = self.check_eligibility(bypass=bypass_eligibility_checks)
        if not eligibility.allowed:
            raise UserIsIneligibleError("The user is not eligible for this event.", eligibility=eligibility)

        self._assert_capacity(use_tickets=False, tier=None)

        rsvp, _created = EventRSVP.objects.update_or_create(
            user=self.user,
            event=self.event,
            defaults={"status": answer},
        )
        return rsvp

    @transaction.atomic
    def create_ticket(
        self, tier: TicketTier, bypass_eligibility_checks: bool = False, price_override: Decimal | None = None
    ) -> Ticket | str:
        """Create a ticket for the user and event.

        Returns:
            Ticket: A ticket for the user and event, or a Stripe checkout URL.

        Raises:
            UserIsIneligibleError
        """
        if not self.event.requires_ticket:
            raise UserIsIneligibleError(
                message="You don't need a ticket for this event.",
                eligibility=EventUserEligibility(
                    allowed=False, event_id=self.event.id, next_step=NextStep.RSVP, reason=_(Reasons.MUST_RSVP)
                ),
            )
        TicketTier.objects.select_for_update().get(pk=tier.pk)
        eligibility = self.check_eligibility(bypass=bypass_eligibility_checks)
        if not eligibility.allowed:
            raise UserIsIneligibleError("The user is not eligible for this event.", eligibility=eligibility)

        # Check membership tier requirement unless waived by invitation
        if not self.eligibility_service.waives_membership_required():
            self._assert_membership_tier_requirement(tier)

        self._assert_capacity(use_tickets=True, tier=tier)

        # Check if user has invitation that waives purchase
        if self.eligibility_service.waives_purchase():
            return self._create_complimentary_ticket(tier)

        ticket_service = TicketService(event=self.event, user=self.user, tier=tier)
        return ticket_service.checkout(price_override=price_override)

    def check_eligibility(self, bypass: bool = False, raise_on_false: bool = False) -> EventUserEligibility:
        """Call the eligibility check.

        Returns:
            EventUserEligibility
        Raises:
            UserIsIneligibleError if the user is not eligible for this event and raise_on_false is True
        """
        eligibility = self.eligibility_service.check_eligibility(bypass=bypass)
        if not eligibility.allowed and raise_on_false:
            raise UserIsIneligibleError(
                message=eligibility.reason or _("You are not eligible."), eligibility=eligibility
            )
        return eligibility

    def _create_complimentary_ticket(self, tier: TicketTier) -> Ticket:
        """Create a complimentary (free) ACTIVE ticket, bypassing payment flow.

        This method is called when a user has an invitation with waives_purchase=True.
        """
        # Increment quantity_sold to respect capacity limits
        TicketTier.objects.select_for_update().filter(pk=tier.pk).update(quantity_sold=F("quantity_sold") + 1)

        # Create an ACTIVE ticket directly, bypassing the payment flow
        ticket = Ticket.objects.create(
            event=self.event,
            tier=tier,
            user=self.user,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name=self.user.get_display_name(),
        )

        return ticket

    def _assert_capacity(self, use_tickets: bool, tier: TicketTier | None) -> None:
        """Raise if the event has no more available attendee slots.

        For ticket events, counts total non-cancelled tickets (each ticket = one attendee).
        For RSVP events, counts YES RSVPs.
        """
        if self.event.max_attendees == 0 or self.eligibility_service.overrides_max_attendees():
            return

        if use_tickets:
            # Count all non-cancelled tickets (each ticket represents one attendee)
            count = (
                Ticket.objects.select_for_update()
                .filter(event=self.event)
                .exclude(status=Ticket.TicketStatus.CANCELLED)
                .count()
            )
            if not tier:
                raise ValueError("Tier must be provided for ticket counts.")
            if tier.total_quantity and tier.quantity_sold >= tier.total_quantity:
                raise UserIsIneligibleError(
                    message="Tier is sold out.",
                    eligibility=EventUserEligibility(
                        allowed=False,
                        event_id=self.event.id,
                        next_step=NextStep.JOIN_WAITLIST if self.event.waitlist_open else None,
                        reason=_(Reasons.SOLD_OUT),
                    ),
                )
        else:
            count = (
                EventRSVP.objects.select_for_update().filter(event=self.event, status=EventRSVP.RsvpStatus.YES).count()
            )

        if count >= self.event.max_attendees:
            raise UserIsIneligibleError(
                message="Event is full.",
                eligibility=EventUserEligibility(
                    allowed=False,
                    event_id=self.event.id,
                    next_step=NextStep.JOIN_WAITLIST if self.event.waitlist_open else None,
                    reason=_(Reasons.EVENT_IS_FULL),
                ),
            )

    def _assert_membership_tier_requirement(self, tier: TicketTier) -> None:
        """Raise if the user doesn't have the required membership tier for this ticket tier.

        Args:
            tier: The ticket tier to check membership requirements for

        Raises:
            UserIsIneligibleError: If the user doesn't have one of the required membership tiers
        """
        # Get required membership tiers for this ticket tier
        required_tier_ids = list(tier.restricted_to_membership_tiers.values_list("id", flat=True))

        # If no tiers are required, allow purchase
        if not required_tier_ids:
            return

        # Check if user has a membership with one of the required tiers
        has_required_tier = OrganizationMember.objects.filter(
            organization=self.event.organization,
            user=self.user,
            tier_id__in=required_tier_ids,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        ).exists()

        if not has_required_tier:
            raise UserIsIneligibleError(
                message="You need to upgrade your membership to purchase this ticket.",
                eligibility=EventUserEligibility(
                    allowed=False,
                    event_id=self.event.id,
                    next_step=NextStep.UPGRADE_MEMBERSHIP,
                    reason=_(Reasons.MEMBERSHIP_TIER_REQUIRED),
                ),
            )
