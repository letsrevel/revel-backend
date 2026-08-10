"""Shared price resolution for wallet passes (Apple and Google rails)."""

from decimal import Decimal

from events.models import Ticket
from events.service.seating.pricing import recorded_or_resolved_price


def resolve_ticket_price(ticket: Ticket) -> tuple[Decimal, str]:
    """Resolve the actual price paid for a ticket.

    Priority:
    1. ticket.price_paid — explicitly recorded for offline/at_the_door PWYC
    2. ticket.payment.amount — online Stripe payment amount
    3. ``recorded_or_resolved_price`` — the seat's price-category price on a
       category-priced tier, else the tier's flat price.

    Step 3 is the same helper the refund ceiling
    (``ticket_service._resolve_offline_refund_amount``) and the revenue report
    (``revenue_aggregation._process_ticket``) use, so the number on the attendee's
    phone cannot disagree with the number they get refunded (#754). It matters for
    exactly one shape: a ticket with no ``price_paid`` and no payment row on a tier
    that is *now* category-priced — reachable when a flat tier opted into category
    pricing after the ticket was sold. Neither branch is purchase-time truth there
    (``tier.price`` is the tier's *current* flat price, not the one in force at the
    sale), so the tie is broken in favour of agreeing with the money-bearing paths.

    Both inputs are already loaded on the paths that generate passes
    (``Ticket.objects.full()`` selects ``seat`` and ``tier``, and the generator
    reads ``ticket.seat.label`` regardless), and the helper is a pure function over
    them — no extra query per pass.

    Returns:
        Tuple of (price, currency).
    """
    tier = ticket.tier
    currency = tier.currency

    if ticket.price_paid is not None:
        return ticket.price_paid, currency

    try:
        payment = ticket.payment
        return payment.amount, payment.currency
    except Ticket.payment.RelatedObjectDoesNotExist:
        pass

    return recorded_or_resolved_price(tier, ticket.seat, None), currency
