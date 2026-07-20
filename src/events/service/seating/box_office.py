"""Box-office door sales, comps, and reseat (spec §2, Phase 4).

Both writers lock the affected ``VenueSeat`` rows PK-ordered under
``select_for_update`` and re-check tickets + overrides + live holds before
writing. ``sell()`` additionally takes the coarse rows first (tier → event)
because it increments ``quantity_sold`` against tier/event capacity; ``reseat()``
changes no aggregate — it only rewrites the ticket's seat/sector, so locking the
two seat rows is sufficient.
"""

import uuid
from decimal import Decimal

from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, EventSeatOverride, Ticket, TicketTier, VenueSeat
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service import BatchTicketService
from events.service.guest import get_or_create_guest_user
from events.service.seating import holds as holds_service
from events.service.seating.pricing import TicketPrice, build_batch_pricing


def resolve_recipient(
    email: str | None, user_id: uuid.UUID | None, first_name: str = "", last_name: str = ""
) -> RevelUser:
    """Resolve the ticket recipient for a box-office sale.

    ``user_id`` wins when given. An email matching ANY existing account (guest or
    not) reuses that account — unlike the self-service guest flow, a staff-driven
    door sale attaching a ticket to a registered user's account is exactly right.
    Unknown emails get a guest user via the existing helper.

    Args:
        email: Recipient email (guest checkout style).
        user_id: Existing RevelUser id.
        first_name: Used only when a new guest user is created.
        last_name: Used only when a new guest user is created.

    Returns:
        The RevelUser the ticket will belong to.
    """
    if user_id is not None:
        return get_object_or_404(RevelUser, pk=user_id)
    assert email is not None  # schema enforces exactly one of email/user_id
    existing = RevelUser.objects.filter(email__iexact=email).first()
    if existing is not None:
        return existing
    return get_or_create_guest_user(email, first_name, last_name)


def _lock_seat_for_sale(event: Event, seat_id: uuid.UUID, recipient: RevelUser) -> VenueSeat:
    """Lock the seat row and re-verify it is sellable; release a box-office HELD override.

    Raises:
        HttpError: 400 for unknown/inactive/wrong-venue/killed seats,
            409 for occupied or foreign-held seats.
    """
    seat = VenueSeat.objects.select_for_update().filter(pk=seat_id).first()
    if seat is None or not seat.is_active or event.venue_id is None or seat.sector.venue_id != event.venue_id:
        raise HttpError(400, str(_("This seat is not available for this event.")))

    # Non-cancelled = occupied, matching the unique_ticket_event_seat constraint.
    if Ticket.objects.filter(event=event, seat=seat).exclude(status=Ticket.TicketStatus.CANCELLED).exists():
        raise HttpError(409, str(_("This seat already has a ticket.")))

    override = EventSeatOverride.objects.filter(event=event, seat=seat).first()
    if override is not None:
        if override.status == EventSeatOverride.OverrideStatus.KILLED:
            raise HttpError(400, str(_("This seat is blocked for this event.")))
        # HELD → sold is what box-office holds are for: release it as part of the sale.
        override.delete()

    try:
        holds_service.verify_and_consume_holds(event, [seat.id], user=recipient, guest_session=None)
    except holds_service.SeatHoldConflictError:
        raise HttpError(409, str(_("This seat is currently held by another buyer."))) from None
    return seat


@transaction.atomic
def sell(
    event: Event,
    tier: TicketTier,
    *,
    seat_id: uuid.UUID,
    payment_method: TicketTier.PaymentMethod,
    recipient: RevelUser,
    guest_name: str | None = None,
) -> Ticket:
    """Issue an ACTIVE ticket directly on a seat (door sale or comp).

    Reuses the purchase path's invariants via ``BatchTicketService``: tier/event
    capacity gates, denormalized venue/sector/seat, ``quantity_sold`` accounting,
    and bulk-create side effects. ``payment_method`` FREE records ``price_paid=0``
    (a comp must not report tier-price revenue); AT_THE_DOOR leaves it null so
    fixed-price reporting falls back to the tier price.

    Args:
        event: The event being sold.
        tier: Tier chosen for price/reporting (must belong to the event — enforced
            by the controller lookup).
        seat_id: The seat to sell.
        payment_method: AT_THE_DOOR or FREE (schema-enforced).
        recipient: The user the ticket belongs to.
        guest_name: Ticket-holder name; defaults to the recipient's display name.

    Returns:
        The created ACTIVE ticket.

    Raises:
        HttpError: On capacity (429/400), seat conflicts (400/409).
    """
    service = BatchTicketService(event, tier, recipient)
    # Coarse locks first (tier → event), matching create_batch's order.
    locked_tier = TicketTier.objects.select_for_update().get(pk=tier.pk)
    service.assert_tier_capacity(locked_tier, 1)
    service.assert_event_capacity(1)

    seat = _lock_seat_for_sale(event, seat_id, recipient)

    item = TicketPurchaseItem(guest_name=guest_name or recipient.get_display_name())
    if payment_method == TicketTier.PaymentMethod.FREE:
        # A comp must not report tier-price revenue.
        lines = [TicketPrice(unit_price=Decimal("0.00"), discount_amount=Decimal("0.00"))]
        stamp_price_paid = True
    else:
        # AT_THE_DOOR: stamp the seat's resolved price when the tier prices per
        # category (tier.price cannot reconstruct it), else leave it null so
        # fixed-price reporting falls back to the tier price.
        lines = build_batch_pricing(locked_tier, [seat]).lines
        stamp_price_paid = bool(locked_tier.category_prices)
    tickets = service.create_tickets(
        [item], [seat], Ticket.TicketStatus.ACTIVE, lines, stamp_price_paid=stamp_price_paid
    )
    TicketTier.objects.filter(pk=locked_tier.pk).update(quantity_sold=F("quantity_sold") + 1)
    service.trigger_bulk_create_side_effects(tickets)
    return tickets[0]


@transaction.atomic
def reseat(event: Event, *, ticket_id: uuid.UUID, target_seat_id: uuid.UUID) -> Ticket:
    """Move a PENDING/ACTIVE ticket to another free seat in the same price category.

    v1 restricts reseat to seats whose ``default_price_category`` equals the
    current seat's (cross-category reseat has an unresolved money question —
    spec §8). Both seat rows are locked PK-ordered before re-checking.

    Args:
        event: The event the ticket belongs to.
        ticket_id: The ticket to move.
        target_seat_id: The seat to move it onto.

    Returns:
        The updated ticket.

    Raises:
        HttpError: 400 for invalid ticket state/target, 409 for occupied or
            foreign-held targets.
    """
    ticket = get_object_or_404(Ticket.objects.select_related("seat__sector", "user"), pk=ticket_id, event=event)
    if ticket.status not in (Ticket.TicketStatus.PENDING, Ticket.TicketStatus.ACTIVE):
        raise HttpError(400, str(_("Only pending or active tickets can be reseated.")))
    current = ticket.seat
    if current is None:
        raise HttpError(400, str(_("This ticket has no seat assigned.")))
    if target_seat_id == current.id:
        raise HttpError(400, str(_("The ticket is already on this seat.")))

    locked = {
        s.pk: s
        for s in VenueSeat.objects.filter(pk__in=[current.pk, target_seat_id]).order_by("pk").select_for_update()
    }
    target = locked.get(target_seat_id)
    if target is None or not target.is_active or target.sector.venue_id != current.sector.venue_id:
        raise HttpError(400, str(_("The target seat is not available for this event.")))
    if target.default_price_category_id != current.default_price_category_id:
        raise HttpError(400, str(_("Reseat is limited to seats in the same price category.")))
    if Ticket.objects.filter(event=event, seat=target).exclude(status=Ticket.TicketStatus.CANCELLED).exists():
        raise HttpError(409, str(_("The target seat already has a ticket.")))
    if EventSeatOverride.objects.filter(event=event, seat=target).exists():
        raise HttpError(400, str(_("The target seat is blocked for this event.")))
    try:
        holds_service.verify_and_consume_holds(event, [target.id], user=ticket.user, guest_session=None)
    except holds_service.SeatHoldConflictError:
        raise HttpError(409, str(_("The target seat is currently held by another buyer."))) from None

    ticket.seat = target
    ticket.sector_id = target.sector_id
    ticket.save(update_fields=["seat", "sector"])
    return ticket
