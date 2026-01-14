# src/events/management/commands/bootstrap_helpers/tickets.py
"""Ticket tier creation for bootstrap process."""

import datetime
from datetime import timedelta
from decimal import Decimal

import structlog
from django.utils import timezone

from events import models as events_models

from .base import BootstrapState

logger = structlog.get_logger(__name__)


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
    """Create ML Workshop sold out tier."""
    events_models.TicketTier.objects.create(
        event=state.events["sold_out_workshop"],
        name="Workshop Seat",
        visibility=events_models.TicketTier.Visibility.PUBLIC,
        payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
        purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
        price=Decimal("299.00"),
        currency="EUR",
        total_quantity=20,
        quantity_sold=20,
        sales_start_at=now - timedelta(days=10),
        sales_end_at=now + timedelta(days=27),
        description="Intensive workshop - materials included",
    )


def _create_seated_concert_tiers(state: BootstrapState, now: "datetime.datetime") -> None:
    """Create Seated Concert tiers with reserved seating and offline payment."""
    concert_sector = events_models.VenueSector.objects.get(venue=state.venues["concert_hall"], name="Main Floor")

    events_models.TicketTier.objects.create(
        event=state.events["seated_concert"],
        name="Reserved Seat",
        visibility=events_models.TicketTier.Visibility.PUBLIC,
        payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
        purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
        price=Decimal("75.00"),
        currency="EUR",
        total_quantity=100,
        quantity_sold=0,
        sales_start_at=now,
        sales_end_at=now + timedelta(days=49),
        description="Reserved seating - select your seat during checkout.",
        venue=state.venues["concert_hall"],
        sector=concert_sector,
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
        manual_payment_instructions="""## Payment Instructions

Please transfer **35.00 EUR** to the following account:

- **Bank**: Revel Events Bank
- **IBAN**: AT12 3456 7890 1234 5678
- **BIC**: REVELAT2X
- **Reference**: Your ticket confirmation number

Once your payment is received, your ticket will be activated within 24 hours.
You will receive an email confirmation when your ticket is ready.

**Questions?** Contact us at tickets@revelcollective.example.com
""",
    )
