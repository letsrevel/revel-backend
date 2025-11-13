from decimal import Decimal
from uuid import UUID

from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, Ticket, TicketTier
from events.service import stripe_service


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
                return self._offline_checkout()
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
            event=self.event, tier=self.tier, user=self.user, status=Ticket.TicketStatus.PENDING
        )
        # Notification sent automatically via post_save signal
        return ticket

    @transaction.atomic
    def _free_checkout(self) -> Ticket:
        TicketTier.objects.select_for_update().filter(pk=self.tier.pk).update(quantity_sold=F("quantity_sold") + 1)
        ticket = Ticket.objects.create(
            event=self.event, tier=self.tier, user=self.user, status=Ticket.TicketStatus.ACTIVE
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
    if ticket.status != Ticket.TicketStatus.ACTIVE:
        if not (
            ticket.status == Ticket.TicketStatus.PENDING
            and ticket.tier.payment_method in (TicketTier.PaymentMethod.AT_THE_DOOR, TicketTier.PaymentMethod.OFFLINE)
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
