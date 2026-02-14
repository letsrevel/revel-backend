"""EventManager for handling RSVP and ticket operations."""

from django.db import transaction
from django.utils.translation import gettext as _

from accounts.models import RevelUser
from events import models
from events.models import (
    EventRSVP,
    Ticket,
    TicketTier,
)

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

    def _assert_capacity(self, use_tickets: bool, tier: TicketTier | None) -> None:
        """Raise if the event has no more available attendee slots.

        For ticket events, counts total non-cancelled tickets (each ticket = one attendee).
        For RSVP events, counts YES RSVPs.

        Uses effective_capacity (min of max_attendees and venue.capacity) as the soft limit.
        This can be overridden by invitations with overrides_max_attendees=True.
        """
        effective_cap = self.event.effective_capacity
        if effective_cap == 0 or self.eligibility_service.overrides_max_attendees():
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

        if count >= effective_cap:
            raise UserIsIneligibleError(
                message="Event is full.",
                eligibility=EventUserEligibility(
                    allowed=False,
                    event_id=self.event.id,
                    next_step=NextStep.JOIN_WAITLIST if self.event.waitlist_open else None,
                    reason=_(Reasons.EVENT_IS_FULL),
                ),
            )
