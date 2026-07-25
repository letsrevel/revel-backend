"""The ``price_paid`` stamping invariant, asserted at EVERY ticket-creating entrypoint.

``should_stamp_price_paid`` / ``price_paid_is_admin_entered``
(``events.service.seating.pricing``) are the single authority for whether a created
ticket records its own ``price_paid`` (spec §5.5). Nothing at the database layer enforces
it — a ticket-writing path that forgets the helper silently leaves ``price_paid`` NULL,
and every money-bearing reader then reconstructs the amount from ``tier.price``, which is
wrong the moment the tier prices its seats by category. This module is the registry of
those paths and pins each one's outcome, so a future path that skips the helper fails a
test here.

The decisive case is a **category-priced** tier: the seat's resolved price is not
``tier.price``, so a skipped stamp is observable (NULL where a number is required). The
mirror case is a **flat** tier, where NULL is the correct, truthful claim.

Registry of ticket-creating entrypoints and where each is pinned:

- **Authenticated batch checkout** (``BatchTicketService.create_batch``) — here, and
  ``test_category_pricing_checkout.py``.
- **Guest checkout** (``guest.confirm_guest_action`` / ``guest.handle_guest_ticket_checkout``,
  both funnel through ``create_batch``) — ``test_controllers/test_guest_user/test_guest_price_paid.py``.
- **Box-office sell** (``seating.box_office.sell``) — here, and ``test_service/test_box_office.py``.
- **Box-office reseat** (``seating.box_office.reseat``) — creates no ticket; it must
  PRESERVE the stamped ``price_paid`` (the same-category constraint is what keeps it
  truthful). Pinned here and in ``test_service/test_box_office.py``.
- **Online checkout** (``_online_checkout``) — the PERMANENT carve-out (#758): never
  stamps, because the 1:1 ``Payment`` row is authoritative and net for reverse-charge
  buyers. Pinned here and in ``test_online_checkout_price_paid.py``.
- **Series-pass materialisation** (``series_pass_service.materialize_tickets``) — out of
  the helper's scope by design: its ``price_paid`` is a share of the pass price that no
  tier reconstructs, so it always stamps. Covered under ``test_series_pass/``.

Any NEW ticket-writing path must be added to this registry with its stamping assertion.
"""

import typing as t
from decimal import Decimal

import pytest

from accounts.models import RevelUser
from events.models import Event, PriceCategory, Ticket, TicketTier, VenueSeat, VenueSector
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service import BatchTicketService
from events.service.seating import box_office
from events.tests.test_service.test_batch_ticket_service.conftest import (
    FLAT,
    PREMIUM,
    STANDARD,
    make_category_tier,
)

pytestmark = pytest.mark.django_db


def _tickets(result: list[Ticket] | tuple[list[Ticket], t.Any]) -> list[Ticket]:
    return result[0] if isinstance(result, tuple) else result


def _flat_tier(event: Event, sector: VenueSector, method: TicketTier.PaymentMethod) -> TicketTier:
    """A flat-priced user-choice tier on the same sector — no category map."""
    return TicketTier.objects.create(
        event=event,
        name=f"Flat {method}",
        price=FLAT,
        currency="EUR",
        payment_method=method,
        total_quantity=50,
        max_tickets_per_user=5,
        seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
        venue=sector.venue,
        sector=sector,
    )


# --- Authenticated batch checkout ------------------------------------------


def test_auth_checkout_category_tier_stamps_seat_price(
    seated_event: Event,
    sector: VenueSector,
    categories: tuple[PriceCategory, PriceCategory],
    seats: list[VenueSeat],
    member_user: RevelUser,
) -> None:
    """Offline checkout on a category-priced tier records each seat's resolved price."""
    tier = make_category_tier(seated_event, sector, categories, TicketTier.PaymentMethod.OFFLINE)
    items = [TicketPurchaseItem(guest_name=f"G{i}", seat_id=s.pk) for i, s in enumerate(seats)]

    tickets = _tickets(BatchTicketService(seated_event, tier, member_user).create_batch(items))

    assert [t.price_paid for t in tickets] == [PREMIUM, STANDARD, FLAT]


def test_auth_checkout_flat_tier_leaves_price_paid_null(
    seated_event: Event,
    sector: VenueSector,
    seats: list[VenueSeat],
    member_user: RevelUser,
) -> None:
    """On a flat tier the NULL claim ("tier.price reconstructs this") is true — do not stamp."""
    tier = _flat_tier(seated_event, sector, TicketTier.PaymentMethod.OFFLINE)
    items = [TicketPurchaseItem(guest_name=f"G{i}", seat_id=s.pk) for i, s in enumerate(seats)]

    tickets = _tickets(BatchTicketService(seated_event, tier, member_user).create_batch(items))

    assert [t.price_paid for t in tickets] == [None, None, None]


def test_online_checkout_category_tier_never_stamps(
    seated_event: Event,
    sector: VenueSector,
    categories: tuple[PriceCategory, PriceCategory],
    seats: list[VenueSeat],
    member_user: RevelUser,
) -> None:
    """The permanent online carve-out (#758): Payment.amount is authoritative, price_paid stays NULL."""
    tier = make_category_tier(seated_event, sector, categories, TicketTier.PaymentMethod.ONLINE)
    items = [TicketPurchaseItem(guest_name=f"G{i}", seat_id=s.pk) for i, s in enumerate(seats)]

    tickets = _tickets(BatchTicketService(seated_event, tier, member_user).create_batch(items))

    assert [t.price_paid for t in tickets] == [None, None, None]


# --- Box-office sell (admin issuance) --------------------------------------


def test_box_office_sell_category_tier_stamps_seat_price(
    seated_event: Event,
    sector: VenueSector,
    categories: tuple[PriceCategory, PriceCategory],
    seats: list[VenueSeat],
    member_user: RevelUser,
) -> None:
    """A door sale of a Premium seat on a category-priced tier records 80.00, not tier.price."""
    tier = make_category_tier(seated_event, sector, categories, TicketTier.PaymentMethod.AT_THE_DOOR)

    ticket = box_office.sell(
        seated_event,
        tier,
        seat_id=seats[0].pk,
        payment_method=TicketTier.PaymentMethod.AT_THE_DOOR,
        recipient=member_user,
    )

    assert ticket.price_paid == PREMIUM


def test_box_office_comp_records_zero(
    seated_event: Event,
    sector: VenueSector,
    categories: tuple[PriceCategory, PriceCategory],
    seats: list[VenueSeat],
    member_user: RevelUser,
) -> None:
    """A comp is a giveaway: 0.00 recorded, whatever the seat's category is worth."""
    tier = make_category_tier(seated_event, sector, categories, TicketTier.PaymentMethod.AT_THE_DOOR)

    ticket = box_office.sell(
        seated_event,
        tier,
        seat_id=seats[0].pk,
        payment_method=TicketTier.PaymentMethod.FREE,
        recipient=member_user,
    )

    assert ticket.price_paid == Decimal("0.00")


def test_box_office_sell_flat_tier_leaves_price_paid_null(
    seated_event: Event,
    sector: VenueSector,
    seats: list[VenueSeat],
    member_user: RevelUser,
) -> None:
    """A flat at-the-door sale keeps NULL — tier.price reconstructs it for reporting."""
    tier = _flat_tier(seated_event, sector, TicketTier.PaymentMethod.AT_THE_DOOR)

    ticket = box_office.sell(
        seated_event,
        tier,
        seat_id=seats[0].pk,
        payment_method=TicketTier.PaymentMethod.AT_THE_DOOR,
        recipient=member_user,
    )

    assert ticket.price_paid is None


# --- Box-office reseat (preserves, creates nothing) ------------------------


def test_box_office_reseat_preserves_stamped_price_paid(
    seated_event: Event,
    sector: VenueSector,
    categories: tuple[PriceCategory, PriceCategory],
    seats: list[VenueSeat],
    member_user: RevelUser,
) -> None:
    """Reseat within the same category must keep the stamped price truthful after the move."""
    premium, _standard = categories
    target = VenueSeat.objects.create(
        sector=sector,
        label="A9",
        row_label="A",
        number=9,
        adjacency_index=8,
        is_active=True,
        default_price_category=premium,
    )
    tier = make_category_tier(seated_event, sector, categories, TicketTier.PaymentMethod.AT_THE_DOOR)
    sold = box_office.sell(
        seated_event,
        tier,
        seat_id=seats[0].pk,
        payment_method=TicketTier.PaymentMethod.AT_THE_DOOR,
        recipient=member_user,
    )
    assert sold.price_paid == PREMIUM

    moved = box_office.reseat(seated_event, ticket_id=sold.pk, target_seat_id=target.pk)

    assert moved.seat_id == target.pk
    assert moved.price_paid == PREMIUM
