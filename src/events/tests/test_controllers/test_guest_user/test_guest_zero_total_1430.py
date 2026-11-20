"""Zero-total guest carts need email confirmation, and tickets record provenance (#1430).

A guest's ONLINE cart that the buyer's own input drives to 0.00 (a 100% code, or PWYC plus a code) used to
reroute to the free checkout and hand back ACTIVE tickets with no email proof. It now follows the
free-guest flow: no rows until the emailed link is clicked. A paid guest checkout stays PENDING and
records ``unconfirmed_guest=True``, which confers no standing (ADR-0020).
"""

import typing as t
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from ninja.errors import HttpError
from ninja_jwt.token_blacklist.models import BlacklistedToken

from accounts.models import RevelUser
from events import schema
from events.exceptions import GuestEmailConfirmationRequired
from events.models import DiscountCode, Event, EventRSVP, Payment, Ticket, TicketTier
from events.service import guest as guest_service
from events.service.batch_ticket_service import BatchTicketService, CartGroup

pytestmark = pytest.mark.django_db

EMAIL = "zero@example.com"


def _code(event: Event, **overrides: t.Any) -> DiscountCode:
    fields: dict[str, t.Any] = {
        "code": "FREE100",
        "organization": event.organization,
        "discount_type": DiscountCode.DiscountType.PERCENTAGE,
        "discount_value": Decimal("100.00"),
        "currency": "EUR",
        "is_active": True,
        "max_uses_per_user": 10,
    }
    fields.update(overrides)
    return DiscountCode.objects.create(**fields)


def _checkout(
    event: Event,
    tier: TicketTier,
    *,
    code: str | None = None,
    email: str = EMAIL,
) -> schema.GuestCheckoutResponseSchema:
    group = CartGroup(tier=tier, items=[schema.TicketPurchaseItem(guest_name="Zed")])
    return guest_service.handle_guest_ticket_checkout(event, [group], email, "Ze", "Ro", discount_code=code)


def _run_checkout(
    event: Event, tier: TicketTier, capture: t.Any, **kwargs: t.Any
) -> tuple[schema.GuestCheckoutResponseSchema, Mock]:
    """Run the checkout, firing on_commit callbacks, with the confirmation email mocked."""
    with patch("events.tasks.send_guest_ticket_confirmation.delay") as send, capture(execute=True):
        response = _checkout(event, tier, **kwargs)
    return response, send


def _assert_asks_for_email(response: schema.GuestCheckoutResponseSchema, send: Mock) -> None:
    assert response.message is not None
    assert response.tickets == []
    assert response.requires_payment is False
    assert response.reservation_id is None
    assert response.checkout_url is None
    send.assert_called_once()


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"discount_type": DiscountCode.DiscountType.FIXED_AMOUNT, "discount_value": Decimal("20.00")},
    ],
    ids=["percentage-100", "fixed-amount-equals-price"],
)
def test_zero_total_online_cart_asks_for_email_and_writes_nothing(
    guest_event_with_tickets: Event,
    online_tier: TicketTier,
    django_capture_on_commit_callbacks: t.Any,
    overrides: dict[str, t.Any],
) -> None:
    code = _code(guest_event_with_tickets, **overrides)

    response, send = _run_checkout(
        guest_event_with_tickets, online_tier, django_capture_on_commit_callbacks, code="FREE100"
    )

    _assert_asks_for_email(response, send)
    assert not Ticket.objects.filter(event=guest_event_with_tickets).exists()
    assert not Payment.objects.filter(ticket__event=guest_event_with_tickets).exists()
    code.refresh_from_db()
    online_tier.refresh_from_db()
    assert code.times_used == 0
    assert online_tier.quantity_sold == 0


def test_confirming_the_zero_total_link_issues_active_confirmed_tickets(
    guest_event_with_tickets: Event, online_tier: TicketTier, django_capture_on_commit_callbacks: t.Any
) -> None:
    code = _code(guest_event_with_tickets)
    _, send = _run_checkout(guest_event_with_tickets, online_tier, django_capture_on_commit_callbacks, code="FREE100")
    token = send.call_args[0][1]

    guest_service.confirm_guest_action(token)

    ticket = Ticket.objects.get(event=guest_event_with_tickets)
    assert ticket.status == Ticket.TicketStatus.ACTIVE
    assert ticket.unconfirmed_guest is False
    assert ticket.price_paid == Decimal("0.00")
    code.refresh_from_db()
    assert code.times_used == 1
    assert ticket.discount_code_id == code.pk


def test_code_exhausted_before_the_click_fails_cleanly(
    guest_event_with_tickets: Event, online_tier: TicketTier, django_capture_on_commit_callbacks: t.Any
) -> None:
    code = _code(guest_event_with_tickets, max_uses=1)
    _, send = _run_checkout(guest_event_with_tickets, online_tier, django_capture_on_commit_callbacks, code="FREE100")
    token = send.call_args[0][1]
    DiscountCode.objects.filter(pk=code.pk).update(times_used=1)

    with pytest.raises(HttpError) as exc:
        guest_service.confirm_guest_action(token)

    assert exc.value.status_code == 400
    assert not Ticket.objects.filter(event=guest_event_with_tickets).exists()
    assert not BlacklistedToken.objects.exists()


def test_paid_guest_cart_stays_pending_and_records_provenance(
    guest_event_with_tickets: Event, online_tier: TicketTier
) -> None:
    _code(guest_event_with_tickets, discount_value=Decimal("50.00"))

    response = _checkout(guest_event_with_tickets, online_tier, code="FREE100")

    assert response.requires_payment is True
    ticket = Ticket.objects.get(event=guest_event_with_tickets)
    assert ticket.status == Ticket.TicketStatus.PENDING
    assert ticket.unconfirmed_guest is True


def test_mixed_cart_with_one_group_zeroed_still_requires_payment(
    guest_event_with_tickets: Event,
    online_tier: TicketTier,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """A code that zeroes only one group does not make the cart free: the other group still has to be paid."""
    other_tier = TicketTier.objects.create(
        event=guest_event_with_tickets,
        name="Other Online",
        price=Decimal("30.00"),
        payment_method=TicketTier.PaymentMethod.ONLINE,
        price_type=TicketTier.PriceType.FIXED,
    )
    guest_event_with_tickets.max_tickets_per_user = 5
    guest_event_with_tickets.save(update_fields=["max_tickets_per_user"])
    code = _code(guest_event_with_tickets)
    code.tiers.set([online_tier])
    groups = [
        CartGroup(tier=online_tier, items=[schema.TicketPurchaseItem(guest_name="Zed")]),
        CartGroup(tier=other_tier, items=[schema.TicketPurchaseItem(guest_name="Yan")]),
    ]

    with (
        patch("events.tasks.send_guest_ticket_confirmation.delay") as send,
        django_capture_on_commit_callbacks(execute=True),
    ):
        response = guest_service.handle_guest_ticket_checkout(
            guest_event_with_tickets, groups, EMAIL, "Ze", "Ro", discount_code="FREE100"
        )

    assert response.requires_payment is True
    assert response.reservation_id is not None
    send.assert_not_called()
    tickets = Ticket.objects.filter(event=guest_event_with_tickets)
    assert tickets.count() == 2
    assert all(ticket.status == Ticket.TicketStatus.PENDING for ticket in tickets)
    assert all(ticket.unconfirmed_guest is True for ticket in tickets)
    zeroed = tickets.get(tier=online_tier)
    # price_paid is not stamped on a PENDING online ticket (set when the payment lands); the discount is.
    assert zeroed.discount_code_id == code.pk
    assert zeroed.discount_amount == online_tier.price
    untouched = tickets.get(tier=other_tier)
    assert untouched.discount_code_id is None
    assert untouched.discount_amount is None


def test_second_confirmation_of_the_same_token_is_refused_even_when_the_pre_check_is_raced(
    guest_event_with_tickets: Event, online_tier: TicketTier, django_capture_on_commit_callbacks: t.Any
) -> None:
    """Two concurrent clicks both pass the blacklist pre-check; the burn itself must refuse the loser."""
    _code(guest_event_with_tickets)
    _, send = _run_checkout(guest_event_with_tickets, online_tier, django_capture_on_commit_callbacks, code="FREE100")
    token = send.call_args[0][1]
    payload = guest_service.validate_and_decode_guest_token(token)
    guest_service.confirm_guest_action(token)
    Ticket.objects.all().delete()

    with patch.object(guest_service, "validate_and_decode_guest_token", return_value=payload):
        with pytest.raises(HttpError) as exc:
            guest_service.confirm_guest_action(token)

    assert exc.value.status_code == 401
    assert not Ticket.objects.filter(event=guest_event_with_tickets).exists()


def test_service_raises_for_unconfirmed_guest_on_non_online_tier_and_writes_nothing(
    guest_event_with_tickets: Event, offline_tier: TicketTier, existing_guest_user: RevelUser
) -> None:
    service = BatchTicketService(
        guest_event_with_tickets,
        user=existing_guest_user,
        groups=[CartGroup(tier=offline_tier, items=[schema.TicketPurchaseItem(guest_name="Zed")])],
        unconfirmed_guest=True,
    )

    with pytest.raises(GuestEmailConfirmationRequired):
        service.create_batch()

    offline_tier.refresh_from_db()
    assert offline_tier.quantity_sold == 0
    assert not Ticket.objects.filter(event=guest_event_with_tickets).exists()


def test_authenticated_zero_total_cart_is_unchanged(
    guest_event_with_tickets: Event, online_tier: TicketTier, existing_regular_user: RevelUser
) -> None:
    code = _code(guest_event_with_tickets)
    service = BatchTicketService(
        guest_event_with_tickets,
        user=existing_regular_user,
        groups=[CartGroup(tier=online_tier, items=[schema.TicketPurchaseItem(guest_name="Zed")])],
        discount_code=code,
    )

    result = service.create_batch()

    assert isinstance(result, list)
    assert result[0].status == Ticket.TicketStatus.ACTIVE
    assert result[0].unconfirmed_guest is False


def test_second_confirmation_of_the_same_rsvp_token_is_refused_even_when_the_pre_check_is_raced(
    guest_event: Event, existing_guest_user: RevelUser
) -> None:
    """The RSVP branch burns the token too: the loser of two concurrent clicks gets 401 and no second RSVP."""
    token = guest_service.create_guest_rsvp_token(existing_guest_user, guest_event.id, "yes")
    payload = guest_service.validate_and_decode_guest_token(token)
    guest_service.confirm_guest_action(token)
    EventRSVP.objects.all().delete()

    with patch.object(guest_service, "validate_and_decode_guest_token", return_value=payload):
        with pytest.raises(HttpError) as exc:
            guest_service.confirm_guest_action(token)

    assert exc.value.status_code == 401
    assert not EventRSVP.objects.filter(event=guest_event).exists()
