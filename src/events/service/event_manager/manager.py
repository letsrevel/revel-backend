"""EventManager for handling RSVP and ticket operations."""

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from accounts.models import RevelUser
from events import models
from events.models import (
    EventRSVP,
    Ticket,
    TicketTier,
)
from events.service.waitlist_service import enqueue_waitlist_processing

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
                    reason_code=Reasons.REQUIRES_TICKET.code,
                ),
            )

        # Users who already RSVP'd YES can freely change to MAYBE/NO
        # This prevents them from being "trapped" if eligibility requirements change
        has_yes_rsvp = EventRSVP.objects.filter(
            user=self.user, event=self.event, status=EventRSVP.RsvpStatus.YES
        ).exists()
        if has_yes_rsvp:
            bypass_eligibility_checks = True

        # Capacity/availability only gates seat-claiming writes (a brand-new YES).
        # NO/MAYBE never consume a seat, and downgrades (YES -> NO/MAYBE) free one, so
        # they must go through even on a full event — otherwise attendees can't free
        # their seat exactly when a waitlist is waiting for it (#691). Every other
        # eligibility gate still applies to NO/MAYBE.
        claims_seat = answer == EventRSVP.RsvpStatus.YES and not has_yes_rsvp
        eligibility = self.check_eligibility(bypass=bypass_eligibility_checks, skip_availability=not claims_seat)
        if not eligibility.allowed:
            raise UserIsIneligibleError("The user is not eligible for this event.", eligibility=eligibility)

        if claims_seat:
            self._assert_capacity(use_tickets=False, tier=None)

        rsvp, _created = EventRSVP.objects.update_or_create(
            user=self.user,
            event=self.event,
            defaults={"status": answer},
        )
        if answer == EventRSVP.RsvpStatus.YES:
            self._claim_active_offer()
        elif has_yes_rsvp:
            # YES -> non-YES frees a seat; trigger next waitlist batch.
            enqueue_waitlist_processing(self.event.id)
        return rsvp

    def _claim_active_offer(self) -> None:
        """Mark the user's active waitlist offer as CLAIMED if any.

        Must be called inside an active transaction after the user has been
        confirmed registered (RSVP YES or non-cancelled ticket). Idempotent —
        no-op when the user has no pending unexpired offer for this event.
        """
        from events.models import EventWaitList, WaitlistOffer

        now = timezone.now()
        offer = (
            WaitlistOffer.objects.select_for_update()
            .filter(
                event=self.event,
                user=self.user,
                status=WaitlistOffer.WaitlistOfferStatus.PENDING,
                expires_at__gt=now,
            )
            .first()
        )
        if offer is None:
            return
        offer.status = WaitlistOffer.WaitlistOfferStatus.CLAIMED
        offer.claimed_at = now
        offer.save(update_fields=["status", "claimed_at"])
        EventWaitList.objects.filter(event=self.event, user=self.user).delete()
        # Defensive nudge: claiming an offer may have freed capacity for the
        # next user (e.g., a batch where one user claimed but others didn't
        # would otherwise leave seats stranded). The processor short-circuits
        # when there's no work, so this is cheap when idempotent.
        enqueue_waitlist_processing(self.event.id)

    def check_eligibility(
        self, bypass: bool = False, raise_on_false: bool = False, skip_availability: bool = False
    ) -> EventUserEligibility:
        """Call the eligibility check.

        Args:
            bypass: When True, skip all eligibility gates.
            raise_on_false: When True, raise ``UserIsIneligibleError`` if not eligible.
            skip_availability: When True, skip the capacity/availability gate (for RSVP
                writes that don't claim a seat).

        Returns:
            EventUserEligibility
        Raises:
            UserIsIneligibleError if the user is not eligible for this event and raise_on_false is True
        """
        eligibility = self.eligibility_service.check_eligibility(bypass=bypass, skip_availability=skip_availability)
        if not eligibility.allowed and raise_on_false:
            raise UserIsIneligibleError(
                message=eligibility.reason or _("You are not eligible."), eligibility=eligibility
            )
        return eligibility

    def _assert_capacity(self, use_tickets: bool, tier: TicketTier | None) -> None:
        """Raise if the event has no more available attendee slots.

        Counts committed attendees PLUS pending unexpired WaitlistOffers
        (minus the current user's own offer, if any). Pending offers reserve
        capacity for waitlist batches, so non-offer-holders see "event is full"
        even when raw attendee counts are below capacity.

        For ticket events, counts total non-cancelled tickets. For RSVP events,
        counts YES RSVPs. Uses effective_capacity (min of max_attendees and
        venue.capacity).
        """
        from events.models import WaitlistOffer

        effective_cap = self.event.effective_capacity
        if effective_cap == 0 or self.eligibility_service.overrides_max_attendees():
            return

        # Lock the Event row to serialize with process_waitlist_for_event and
        # other capacity-modifying flows (e.g. BatchTicketService).
        self.event = models.Event.objects.select_for_update().get(pk=self.event.pk)

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
                        reason_code=Reasons.SOLD_OUT.code,
                    ),
                )
        else:
            count = (
                EventRSVP.objects.select_for_update().filter(event=self.event, status=EventRSVP.RsvpStatus.YES).count()
            )

        now = timezone.now()
        # Cutoff-batch offers don't reserve capacity (they race FCFS against real seats),
        # so they are excluded from both pending counts here.
        pending_offers = (
            WaitlistOffer.objects.select_for_update()
            .filter(
                event=self.event,
                status=WaitlistOffer.WaitlistOfferStatus.PENDING,
                expires_at__gt=now,
                is_cutoff_batch=False,
            )
            .count()
        )
        has_own_offer = WaitlistOffer.objects.filter(
            event=self.event,
            user=self.user,
            status=WaitlistOffer.WaitlistOfferStatus.PENDING,
            expires_at__gt=now,
            is_cutoff_batch=False,
        ).exists()
        if has_own_offer:
            pending_offers = max(0, pending_offers - 1)

        if count + pending_offers >= effective_cap:
            reason = (
                Reasons.SPOTS_RESERVED_FOR_WAITLIST
                if pending_offers > 0 and count < effective_cap
                else Reasons.EVENT_IS_FULL
            )
            raise UserIsIneligibleError(
                message="Event is full.",
                eligibility=EventUserEligibility(
                    allowed=False,
                    event_id=self.event.id,
                    next_step=NextStep.JOIN_WAITLIST if self.event.waitlist_open else None,
                    reason=_(reason),
                    reason_code=reason.code,
                ),
            )
