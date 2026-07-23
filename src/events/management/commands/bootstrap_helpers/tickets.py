# src/events/management/commands/bootstrap_helpers/tickets.py
"""Ticket tier creation for bootstrap process."""

import datetime
from datetime import timedelta
from decimal import Decimal

import structlog
from django.utils import timezone

from accounts.models import RevelUser
from events import models as events_models

from .base import BootstrapState

logger = structlog.get_logger(__name__)

_OFFLINE_INSTRUCTIONS = """## Payment Instructions

Please transfer **{price} {currency}** to the following account:

- **Bank**: Revel Events Bank
- **IBAN**: AT12 3456 7890 1234 5678
- **BIC**: REVELAT2X
- **Reference**: Your ticket confirmation number

Once your payment is received, your ticket will be activated within 24 hours.
You will receive an email confirmation when your ticket is ready.

**Questions?** Contact us at tickets@revelcollective.example.com
"""

_EVENT_CURRENCIES: dict[str, str] = {
    "summer_festival": "USD",
    "wine_tasting": "USD",
    "tech_conference": "EUR",
    "wellness_retreat": "EUR",
    "past_event": "USD",
    "sold_out_workshop": "EUR",
    "draft_event": "EUR",
    "seated_concert": "EUR",
}


def create_ticket_tiers(state: BootstrapState) -> None:
    """Create diverse ticket tiers for events."""
    logger.info("Creating ticket tiers...")

    now = timezone.now()

    # Delete auto-created default tiers for events that will have custom tiers
    # (except summer_festival which uses the default "General Admission" tier)
    events_with_custom_tiers = [
        state.events["wine_tasting"],
        state.events["tech_conference"],
        state.events["wellness_retreat"],
        state.events["past_event"],
        state.events["sold_out_workshop"],
        state.events["draft_event"],
        state.events["seated_concert"],
    ]
    events_models.TicketTier.objects.filter(
        event__in=events_with_custom_tiers,
        name=events_models.DEFAULT_TICKET_TIER_NAME,
    ).delete()

    _create_summer_festival_tiers(state, now)
    _create_wine_tasting_tier(state, now)
    _create_tech_conference_tiers(state, now)
    _create_wellness_retreat_tiers(state, now)
    _create_past_event_tier(state, now)
    _create_sold_out_workshop_tier(state, now)
    _create_seated_concert_tiers(state, now)

    # Ensure every ticketed event covers all payment method × price type combinations
    _add_comprehensive_tiers(state, now)

    logger.info("Created ticket tiers for events with tickets")


def _create_summer_festival_tiers(state: BootstrapState, now: "datetime.datetime") -> None:
    """Create Summer Festival ticket tiers."""
    events_models.TicketTier.objects.create(
        event=state.events["summer_festival"],
        name="Early Bird General Admission",
        visibility=events_models.TicketTier.Visibility.PUBLIC,
        payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
        purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
        price=Decimal("45.00"),
        currency="USD",
        total_quantity=200,
        quantity_sold=180,
        sales_start_at=now - timedelta(days=30),
        sales_end_at=now + timedelta(days=15),
        description="Early bird pricing - save $20!",
    )

    events_models.TicketTier.objects.filter(name="General Admission", event=state.events["summer_festival"]).update(
        visibility=events_models.TicketTier.Visibility.PUBLIC,
        payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
        purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
        price=Decimal("65.00"),
        currency="USD",
        total_quantity=250,
        quantity_sold=45,
        sales_start_at=now + timedelta(days=15),
        sales_end_at=now + timedelta(days=44),
        description="Standard admission ticket",
    )

    events_models.TicketTier.objects.create(
        event=state.events["summer_festival"],
        name="VIP Experience",
        visibility=events_models.TicketTier.Visibility.PUBLIC,
        payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
        purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
        price=Decimal("150.00"),
        currency="USD",
        total_quantity=50,
        quantity_sold=12,
        sales_start_at=now - timedelta(days=30),
        sales_end_at=now + timedelta(days=44),
        description="""VIP perks include:
- Priority entry
- VIP lounge access with premium bar
- Meet & greet with artists
- Exclusive merchandise
- Premium viewing area
""",
    )


def _create_wine_tasting_tier(state: BootstrapState, now: "datetime.datetime") -> None:
    """Create Wine Tasting invitation-only tier."""
    events_models.TicketTier.objects.create(
        event=state.events["wine_tasting"],
        name="Exclusive Seating",
        visibility=events_models.TicketTier.Visibility.PRIVATE,
        payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
        purchasable_by=events_models.TicketTier.PurchasableBy.INVITED,
        price=Decimal("200.00"),
        currency="USD",
        total_quantity=40,
        quantity_sold=8,
        sales_start_at=now,
        sales_end_at=now + timedelta(days=29),
        description="Invitation-only exclusive wine tasting dinner",
    )


def _create_tech_conference_tiers(state: BootstrapState, now: "datetime.datetime") -> None:
    """Create Tech Conference tiers with different access levels."""
    events_models.TicketTier.objects.create(
        event=state.events["tech_conference"],
        name="Early Bird - Full Access",
        visibility=events_models.TicketTier.Visibility.PUBLIC,
        payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
        purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
        price=Decimal("399.00"),
        currency="EUR",
        total_quantity=300,
        quantity_sold=295,
        sales_start_at=now - timedelta(days=60),
        sales_end_at=now + timedelta(days=5),
        description="Early bird rate - ends soon! Full 3-day access.",
    )

    events_models.TicketTier.objects.create(
        event=state.events["tech_conference"],
        name="Standard - Full Access",
        visibility=events_models.TicketTier.Visibility.PUBLIC,
        payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
        purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
        price=Decimal("599.00"),
        currency="EUR",
        total_quantity=500,
        quantity_sold=120,
        sales_start_at=now + timedelta(days=5),
        sales_end_at=now + timedelta(days=59),
        description="Full 3-day conference access with all meals included.",
    )

    events_models.TicketTier.objects.create(
        event=state.events["tech_conference"],
        name="Workshop Bundle",
        visibility=events_models.TicketTier.Visibility.PUBLIC,
        payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
        purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
        price=Decimal("899.00"),
        currency="EUR",
        total_quantity=100,
        quantity_sold=34,
        sales_start_at=now,
        sales_end_at=now + timedelta(days=59),
        description="Conference + 2 workshops of your choice. Best value!",
    )

    events_models.TicketTier.objects.create(
        event=state.events["tech_conference"],
        name="Member Discount",
        visibility=events_models.TicketTier.Visibility.MEMBERS_ONLY,
        payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
        purchasable_by=events_models.TicketTier.PurchasableBy.MEMBERS,
        price=Decimal("299.00"),
        currency="EUR",
        total_quantity=100,
        quantity_sold=23,
        sales_start_at=now,
        sales_end_at=now + timedelta(days=59),
        description="Special member-only pricing - 50% off!",
    )


def _create_wellness_retreat_tiers(state: BootstrapState, now: "datetime.datetime") -> None:
    """Create Wellness Retreat tiers including PWYC."""
    events_models.TicketTier.objects.create(
        event=state.events["wellness_retreat"],
        name="Shared Room",
        visibility=events_models.TicketTier.Visibility.PUBLIC,
        payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
        purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
        price=Decimal("250.00"),
        currency="EUR",
        total_quantity=20,
        quantity_sold=14,
        sales_start_at=now,
        sales_end_at=now + timedelta(days=34),
        description="Shared accommodation (2 per room)",
    )

    events_models.TicketTier.objects.create(
        event=state.events["wellness_retreat"],
        name="Community Support Rate",
        visibility=events_models.TicketTier.Visibility.PUBLIC,
        payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
        purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
        price_type=events_models.TicketTier.PriceType.PWYC,
        price=Decimal("150.00"),
        pwyc_min=Decimal("100.00"),
        pwyc_max=Decimal("250.00"),
        currency="EUR",
        total_quantity=5,
        quantity_sold=3,
        sales_start_at=now,
        sales_end_at=now + timedelta(days=34),
        description="Pay what you can - making wellness accessible to all. Shared rooms.",
    )


def _create_past_event_tier(state: BootstrapState, now: "datetime.datetime") -> None:
    """Create past event tier."""
    events_models.TicketTier.objects.create(
        event=state.events["past_event"],
        name="Gala Ticket",
        visibility=events_models.TicketTier.Visibility.PUBLIC,
        payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
        purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
        price=Decimal("250.00"),
        currency="USD",
        total_quantity=200,
        quantity_sold=200,
        sales_start_at=now - timedelta(days=120),
        sales_end_at=now - timedelta(days=91),
        description="Sold out - event has passed",
    )


def _create_sold_out_workshop_tier(state: BootstrapState, now: "datetime.datetime") -> None:
    """Create ML Workshop sold out tier and fill the event with 20 actual tickets.

    Real ticket rows are required so the eligibility pipeline counts the event as
    full (AvailabilityGate / _assert_capacity count non-cancelled tickets). The
    Event's ``attendee_count`` is also pre-set so the waitlist processor sees
    ``available <= 0`` immediately, without waiting for the recompute task.
    """
    workshop = state.events["sold_out_workshop"]
    # Offline payment tier so admins can cancel filler tickets manually during
    # smoke testing without going through Stripe.
    tier = events_models.TicketTier.objects.create(
        event=workshop,
        name="Workshop Seat",
        visibility=events_models.TicketTier.Visibility.PUBLIC,
        payment_method=events_models.TicketTier.PaymentMethod.OFFLINE,
        purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
        price=Decimal("299.00"),
        currency="EUR",
        total_quantity=20,
        quantity_sold=20,
        sales_start_at=now - timedelta(days=10),
        sales_end_at=now + timedelta(days=27),
        description="Intensive workshop - materials included",
        manual_payment_instructions=_OFFLINE_INSTRUCTIONS.format(price="299.00", currency="EUR"),
    )

    # Fill the event with 20 ad-hoc filler users so the named bootstrap users
    # (Alice, George, …) stay free to join the waitlist during smoke testing.
    # The fillers are intentionally not added to state.users — they exist only
    # to occupy seats.
    for i in range(1, 21):
        filler = RevelUser.objects.create_user(
            username=f"ml-filler-{i:02d}@bootstrap.example",
            email=f"ml-filler-{i:02d}@bootstrap.example",
            password="password123",
            email_verified=True,
            first_name="Workshop",
            last_name=f"Attendee {i:02d}",
        )
        events_models.Ticket.objects.create(
            guest_name=filler.get_display_name(),
            event=workshop,
            user=filler,
            tier=tier,
            status=events_models.Ticket.TicketStatus.ACTIVE,
        )


def _create_seated_concert_tiers(state: BootstrapState, now: "datetime.datetime") -> None:
    """Create Seated Concert tiers: zone-priced Orchestra, flat-priced Balcony, flat Standing Room."""
    venue = state.venues["concert_hall"]
    orchestra = events_models.VenueSector.objects.get(venue=venue, name="Orchestra")
    balcony = events_models.VenueSector.objects.get(venue=venue, name="Balcony")
    cat_premium = events_models.PriceCategory.objects.get(venue=venue, name="Orchestra Premium")
    cat_standard = events_models.PriceCategory.objects.get(venue=venue, name="Orchestra Standard")

    reserved_seat = events_models.TicketTier.objects.create(
        event=state.events["seated_concert"],
        name="Reserved Seat",
        visibility=events_models.TicketTier.Visibility.PUBLIC,
        payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
        purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
        price=Decimal("75.00"),
        currency="EUR",
        total_quantity=120,
        quantity_sold=0,
        sales_start_at=now,
        sales_end_at=now + timedelta(days=49),
        description="Reserved seating in the Orchestra - select your seat during checkout.",
        venue=venue,
        sector=orchestra,
        seat_assignment_mode=events_models.TicketTier.SeatAssignmentMode.USER_CHOICE,
        category_prices={
            str(cat_premium.id): "95.00",
            str(cat_standard.id): "75.00",
        },
    )

    balcony_seat = events_models.TicketTier.objects.create(
        event=state.events["seated_concert"],
        name="Balcony Seat",
        visibility=events_models.TicketTier.Visibility.PUBLIC,
        payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
        purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
        price=Decimal("55.00"),
        currency="EUR",
        total_quantity=56,
        quantity_sold=0,
        sales_start_at=now,
        sales_end_at=now + timedelta(days=49),
        description="Reserved seating in the Balcony - select your seat during checkout.",
        venue=venue,
        sector=balcony,
        seat_assignment_mode=events_models.TicketTier.SeatAssignmentMode.USER_CHOICE,
    )

    events_models.TicketTier.objects.create(
        event=state.events["seated_concert"],
        name="Standing Room",
        visibility=events_models.TicketTier.Visibility.PUBLIC,
        payment_method=events_models.TicketTier.PaymentMethod.OFFLINE,
        purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
        price=Decimal("35.00"),
        currency="EUR",
        total_quantity=50,
        quantity_sold=8,
        sales_start_at=now,
        sales_end_at=now + timedelta(days=49),
        description="Standing room at the back of the venue. Pay via bank transfer.",
        manual_payment_instructions=_OFFLINE_INSTRUCTIONS.format(price="35.00", currency="EUR"),
    )

    sold_reserved = _sell_seated_concert_tickets(reserved_seat, occupancy=0.18, filler_offset=0)
    _sell_seated_concert_tickets(balcony_seat, occupancy=0.18, filler_offset=sold_reserved)


def _sell_seated_concert_tickets(tier: events_models.TicketTier, occupancy: float, filler_offset: int) -> int:
    """Sell a deterministic slice of a sector's active seats as active tickets.

    Selection takes an evenly-spaced slice of seats (ordered by row/number)
    rather than a random sample, so bootstrap output is stable across runs.
    Buyers are dedicated filler accounts (not added to ``BootstrapState.users``),
    mirroring the ``ml-filler`` pattern in ``_create_sold_out_workshop_tier`` — so
    the named bootstrap users (Alice, Charlie, ...) stay untouched and free to
    test purchasing on this event. ``filler_offset`` keeps two calls selling
    different tiers of the same event from reusing filler numbers; pass the
    previous call's return value as the next call's offset.

    Returns:
        The number of tickets sold, for chaining ``filler_offset`` into the next call.
    """
    active_seats = list(
        events_models.VenueSeat.objects.filter(sector=tier.sector, is_active=True).order_by("row_label", "number")
    )
    num_to_sell = max(1, int(len(active_seats) * occupancy))
    step = max(1, len(active_seats) // num_to_sell)
    picked = active_seats[::step][:num_to_sell]

    tickets: list[events_models.Ticket] = []
    for i, seat in enumerate(picked):
        filler_num = filler_offset + i + 1
        filler = RevelUser.objects.create_user(
            username=f"concert-filler-{filler_num:02d}@bootstrap.example",
            email=f"concert-filler-{filler_num:02d}@bootstrap.example",
            password="password123",
            email_verified=True,
            first_name="Concert",
            last_name=f"Attendee {filler_num:02d}",
        )
        tickets.append(
            events_models.Ticket(
                event=tier.event,
                user=filler,
                tier=tier,
                status=events_models.Ticket.TicketStatus.ACTIVE,
                guest_name=filler.get_display_name(),
                venue=tier.venue,
                sector=tier.sector,
                seat=seat,
            )
        )
    events_models.Ticket.objects.bulk_create(tickets)
    tier.quantity_sold = len(tickets)
    tier.save(update_fields=["quantity_sold"])
    logger.info(f"  {tier.event.name} / {tier.name}: {len(tickets)} active tickets sold")
    return len(tickets)


def _add_comprehensive_tiers(state: BootstrapState, now: "datetime.datetime") -> None:
    """Ensure every ticketed event has a tier for each payment method × price type combination.

    Covers:
    - ONLINE × FIXED (already present for most events)
    - ONLINE × PWYC
    - OFFLINE × FIXED (seated_concert already has this)
    - OFFLINE × PWYC
    - AT_THE_DOOR × FIXED
    - AT_THE_DOOR × PWYC
    - FREE × FIXED
    """
    logger.info("Adding comprehensive tier combinations to all ticketed events...")

    TM = events_models.TicketTier.PaymentMethod
    PT = events_models.TicketTier.PriceType

    # (payment_method, price_type, name, price, extra_kwargs)
    combos: list[tuple[str, str, str, Decimal, dict[str, object]]] = [
        (
            TM.ONLINE,
            PT.PWYC,
            "Pay What You Can (Online)",
            Decimal("10.00"),
            {
                "pwyc_min": Decimal("5.00"),
                "pwyc_max": Decimal("50.00"),
                "description": "Pay what you can to support this event (online payment).",
            },
        ),
        (
            TM.OFFLINE,
            PT.FIXED,
            "Offline Payment (Fixed)",
            Decimal("20.00"),
            {"description": "Fixed-price ticket — pay via bank transfer."},
        ),
        (
            TM.OFFLINE,
            PT.PWYC,
            "Pay What You Can (Bank Transfer)",
            Decimal("10.00"),
            {
                "pwyc_min": Decimal("5.00"),
                "pwyc_max": Decimal("50.00"),
                "description": "Pay what you can via bank transfer.",
            },
        ),
        (
            TM.AT_THE_DOOR,
            PT.FIXED,
            "At The Door",
            Decimal("15.00"),
            {"description": "Pay a fixed price at the door on arrival."},
        ),
        (
            TM.AT_THE_DOOR,
            PT.PWYC,
            "Pay What You Can (At The Door)",
            Decimal("10.00"),
            {
                "pwyc_min": Decimal("5.00"),
                "pwyc_max": Decimal("50.00"),
                "description": "Pay what you can at the door on arrival.",
            },
        ),
        (
            TM.FREE,
            PT.FIXED,
            "Free Admission",
            Decimal("0.00"),
            {"description": "Free entry — no payment required."},
        ),
    ]

    for event_key, event in state.events.items():
        if not event.requires_ticket:
            continue

        currency = _EVENT_CURRENCIES.get(event_key, "EUR")
        existing: set[tuple[str, str]] = set(event.ticket_tiers.values_list("payment_method", "price_type"))

        for payment_method, price_type, name, price, extra in combos:
            if (payment_method, price_type) in existing:
                continue

            manual_instructions: str | None = None
            if payment_method == TM.OFFLINE:
                manual_instructions = _OFFLINE_INSTRUCTIONS.format(price=price, currency=currency)

            events_models.TicketTier.objects.create(
                event=event,
                name=name,
                visibility=events_models.TicketTier.Visibility.PUBLIC,
                payment_method=payment_method,
                price_type=price_type,
                purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
                price=price,
                currency=currency,
                total_quantity=10,
                quantity_sold=0,
                sales_start_at=event.start - timedelta(days=30),
                sales_end_at=event.start + timedelta(hours=12),
                manual_payment_instructions=manual_instructions,
                **extra,
            )
