from decimal import Decimal
from uuid import UUID

from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, Ticket, TicketTier
from events.service import stripe_service
from events.service.ticket_notification_service import notify_ticket_creation


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
            raise HttpError(400, "You already have a ticket.")
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
                raise HttpError(400, "Unknown payment method.")

    def _stripe_checkout(self, price_override: Decimal | None = None) -> str:
        checkout_url, _ = stripe_service.create_checkout_session(
            self.event, self.tier, self.user, price_override=price_override
        )
        return checkout_url

    @transaction.atomic
    def _offline_checkout(self) -> Ticket:
        TicketTier.objects.select_for_update().filter(pk=self.tier.pk).update(quantity_sold=F("quantity_sold") + 1)
        ticket = Ticket.objects.create(event=self.event, tier=self.tier, user=self.user, status=Ticket.Status.PENDING)

        # Send notification for ticket creation
        notify_ticket_creation(str(ticket.id))

        return ticket

    @transaction.atomic
    def _free_checkout(self) -> Ticket:
        TicketTier.objects.select_for_update().filter(pk=self.tier.pk).update(quantity_sold=F("quantity_sold") + 1)
        ticket = Ticket.objects.create(event=self.event, tier=self.tier, user=self.user, status=Ticket.Status.ACTIVE)

        # Send notification for free ticket creation
        notify_ticket_creation(str(ticket.id))

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
    err_msg = {
        Ticket.Status.CHECKED_IN: "This ticket has already been checked in.",
        Ticket.Status.CANCELLED: "This ticket has been cancelled.",
        Ticket.Status.PENDING: "This ticket is pending payment confirmation.",
    }
    if ticket.status != Ticket.Status.ACTIVE:
        if not (
            ticket.status == Ticket.Status.PENDING
            and ticket.tier.payment_method in (TicketTier.PaymentMethod.AT_THE_DOOR, TicketTier.PaymentMethod.OFFLINE)
        ):
            raise HttpError(400, err_msg.get(ticket.status, f"Invalid ticket status: {ticket.status}"))  # type: ignore[call-overload]

    # Check if check-in window is open
    if not event.is_check_in_open():
        raise HttpError(400, "Check-in is not currently open for this event.")

    # Update ticket status
    ticket.status = Ticket.Status.CHECKED_IN
    ticket.checked_in_at = timezone.now()
    ticket.checked_in_by = checked_in_by
    ticket.save(update_fields=["status", "checked_in_at", "checked_in_by"])

    return ticket
