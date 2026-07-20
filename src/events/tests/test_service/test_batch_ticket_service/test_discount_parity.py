"""Byte-identical parity for discounted purchases on a **flat** tier (plan Task 5).

Task 5 narrows ``create_batch``'s scalar to PWYC and moves discount math into
``events.service.seating.pricing``. At this point no tier carries a
``category_prices`` map, so the change must be a *pure refactor*: every ticket
and every ``Payment`` row a discounted purchase produces must be unchanged.

These tests are written against the **pre-refactor** behaviour and are expected
to pass before and after. The expected values are hand-computed from the code
that produced them:

- ``Ticket.price_paid`` — ``price_override`` for offline/at-the-door
  (``batch_ticket_service.py:884,915``), ``None`` for online (price lives on
  ``Payment.amount``) and ``None`` when nothing overrides the tier price.
- ``Ticket.discount_amount`` — ``calculate_discount_amount(tier, dc)``, stamped
  once per batch (``batch_ticket_service.py:705``); **``None``** when there is no
  discount code. That NULL is load-bearing (it feeds the revenue detail sheet),
  so it is asserted explicitly, not just "falsy".
- ``Payment.amount`` — ``base_price = price_override or tier.price``
  (``stripe_service.py:394``), one row per ticket.
- A FIXED_AMOUNT code equal to ``tier.price`` drives the unit price to zero and
  must reroute an ONLINE tier to the free checkout (no ``Payment`` rows, ACTIVE
  tickets) — today via the scalar shortcut, afterwards via the price vector.

Both call-site families are covered: the authenticated controller
(``event_public/tickets.py``) and **both** guest sites (``guest.py`` checkout and
``guest.py`` token confirmation).
"""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja.errors import HttpError
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events import schema
from events.models import Event, Organization, Payment, Ticket, TicketTier
from events.models.discount_code import DiscountCode
from events.service.guest import confirm_guest_action, handle_guest_ticket_checkout

pytestmark = pytest.mark.django_db

TIER_PRICE = Decimal("25.00")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def parity_org(organization: Organization) -> Organization:
    """Stripe-connected organization so ONLINE reservations can be created."""
    organization.stripe_account_id = "acct_parity"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.platform_fee_percent = Decimal("3.00")
    organization.platform_fee_fixed = Decimal("0.50")
    organization.save()
    return organization


@pytest.fixture
def parity_event(parity_org: Organization) -> Event:
    """Public, open, guest-accessible event."""
    return Event.objects.create(
        organization=parity_org,
        name="Parity Event",
        slug="parity-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        start=timezone.now() + timedelta(days=7),
        status=Event.EventStatus.OPEN,
        max_attendees=100,
        requires_ticket=True,
        can_attend_without_login=True,
    )


def _make_tier(event: Event, method: TicketTier.PaymentMethod, price: Decimal = TIER_PRICE) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name=f"Tier {method}",
        price=price,
        currency="EUR",
        payment_method=method,
        price_type=TicketTier.PriceType.FIXED,
        total_quantity=50,
        max_tickets_per_user=5,
    )


@pytest.fixture
def online_tier(parity_event: Event) -> TicketTier:
    """25.00 EUR online tier."""
    return _make_tier(parity_event, TicketTier.PaymentMethod.ONLINE)


@pytest.fixture
def offline_tier(parity_event: Event) -> TicketTier:
    """25.00 EUR offline tier."""
    return _make_tier(parity_event, TicketTier.PaymentMethod.OFFLINE)


@pytest.fixture
def at_the_door_tier(parity_event: Event) -> TicketTier:
    """25.00 EUR at-the-door tier."""
    return _make_tier(parity_event, TicketTier.PaymentMethod.AT_THE_DOOR)


@pytest.fixture
def free_tier(parity_event: Event) -> TicketTier:
    """Free tier — discount codes are rejected on it."""
    return _make_tier(parity_event, TicketTier.PaymentMethod.FREE, price=Decimal("0.00"))


def _make_code(org: Organization, code: str, kind: DiscountCode.DiscountType, value: str) -> DiscountCode:
    return DiscountCode.objects.create(
        code=code,
        organization=org,
        discount_type=kind,
        discount_value=Decimal(value),
        currency="EUR",
        max_uses_per_user=10,
    )


@pytest.fixture
def pct20(parity_org: Organization) -> DiscountCode:
    """20% off → 25.00 becomes 20.00, discount_amount 5.00."""
    return _make_code(parity_org, "PCT20", DiscountCode.DiscountType.PERCENTAGE, "20.00")


@pytest.fixture
def fix10(parity_org: Organization) -> DiscountCode:
    """10.00 EUR off → 25.00 becomes 15.00, discount_amount 10.00."""
    return _make_code(parity_org, "FIX10", DiscountCode.DiscountType.FIXED_AMOUNT, "10.00")


@pytest.fixture
def fix_full(parity_org: Organization) -> DiscountCode:
    """25.00 EUR off → the whole ticket, driving the unit price to 0.00."""
    return _make_code(parity_org, "FIXFULL", DiscountCode.DiscountType.FIXED_AMOUNT, "25.00")


@pytest.fixture
def buyer_client(organization_owner_user: RevelUser) -> Client:
    """JWT-authenticated client for the checkout endpoint."""
    client = Client()
    refresh = RefreshToken.for_user(organization_owner_user)
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {refresh.access_token}"  # type: ignore[attr-defined]
    return client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _checkout(client: Client, event: Event, tier: TicketTier, *, code: str | None, count: int = 2) -> t.Any:
    """POST the authenticated batch checkout endpoint."""
    url = reverse("api:ticket_checkout", kwargs={"event_id": event.pk, "tier_id": tier.pk})
    payload: dict[str, t.Any] = {"tickets": [{"guest_name": f"Guest {i}"} for i in range(count)]}
    if code is not None:
        payload["discount_code"] = code
    with mock.patch("stripe.checkout.Session.create") as create:
        response = client.post(url, data=payload, content_type="application/json")
        create.assert_not_called()
    return response


def _assert_tickets(
    tickets: list[Ticket],
    *,
    status: Ticket.TicketStatus,
    price_paid: Decimal | None,
    discount_amount: Decimal | None,
) -> None:
    """Every ticket in the batch carries exactly these money columns."""
    assert tickets
    for ticket in tickets:
        assert ticket.status == status
        assert ticket.price_paid == price_paid
        assert ticket.discount_amount == discount_amount


# ---------------------------------------------------------------------------
# Authenticated checkout — event_public/tickets.py call site
# ---------------------------------------------------------------------------


class TestAuthenticatedOnlineParity:
    """ONLINE tier: the discounted unit price must land on every Payment row."""

    @pytest.mark.parametrize(
        "code_fixture,expected_amount,expected_discount",
        [
            (None, Decimal("25.00"), None),
            ("pct20", Decimal("20.00"), Decimal("5.00")),
            ("fix10", Decimal("15.00"), Decimal("10.00")),
        ],
    )
    def test_online_payment_rows(
        self,
        request: pytest.FixtureRequest,
        buyer_client: Client,
        parity_event: Event,
        online_tier: TicketTier,
        code_fixture: str | None,
        expected_amount: Decimal,
        expected_discount: Decimal | None,
    ) -> None:
        """One PENDING Payment per ticket at the discounted unit price; price_paid stays NULL."""
        code = None if code_fixture is None else t.cast(DiscountCode, request.getfixturevalue(code_fixture)).code
        response = _checkout(buyer_client, parity_event, online_tier, code=code)

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["requires_payment"] is True
        reservation_id = body["reservation_id"]

        payments = list(Payment.objects.filter(reservation_id=reservation_id).order_by("created_at"))
        assert len(payments) == 2
        assert [p.amount for p in payments] == [expected_amount, expected_amount]
        assert all(p.status == Payment.PaymentStatus.PENDING for p in payments)
        assert all(p.currency == "EUR" for p in payments)
        # Platform fee: 3% of the batch total + 0.50 fixed, distributed per ticket.
        total = expected_amount * 2
        expected_fee_total = (total * Decimal("3.00") / Decimal(100)).quantize(Decimal("0.01")) + Decimal("0.50")
        assert sum((p.platform_fee for p in payments), Decimal("0")) == expected_fee_total

        tickets = list(Ticket.objects.filter(id__in=[p.ticket_id for p in payments]))
        _assert_tickets(
            tickets,
            status=Ticket.TicketStatus.PENDING,
            price_paid=None,
            discount_amount=expected_discount,
        )

    def test_full_fixed_amount_code_reroutes_to_free_checkout(
        self,
        buyer_client: Client,
        parity_event: Event,
        online_tier: TicketTier,
        fix_full: DiscountCode,
    ) -> None:
        """A code worth the whole ticket makes the online batch free: ACTIVE tickets, no Payments."""
        response = _checkout(buyer_client, parity_event, online_tier, code=fix_full.code)

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["requires_payment"] is False
        assert len(body["tickets"]) == 2
        assert body["reservation_id"] is None

        tickets = list(Ticket.objects.filter(tier=online_tier))
        assert len(tickets) == 2
        _assert_tickets(
            tickets,
            status=Ticket.TicketStatus.ACTIVE,
            price_paid=None,
            discount_amount=Decimal("25.00"),
        )
        assert not Payment.objects.filter(ticket__tier=online_tier).exists()


class TestAuthenticatedNonOnlineParity:
    """OFFLINE / AT_THE_DOOR / FREE: the discounted unit price lands on ``price_paid``."""

    @pytest.mark.parametrize(
        "code_fixture,expected_price_paid,expected_discount",
        [
            (None, None, None),
            ("pct20", Decimal("20.00"), Decimal("5.00")),
            ("fix10", Decimal("15.00"), Decimal("10.00")),
            ("fix_full", Decimal("0.00"), Decimal("25.00")),
        ],
    )
    def test_offline(
        self,
        request: pytest.FixtureRequest,
        buyer_client: Client,
        parity_event: Event,
        offline_tier: TicketTier,
        code_fixture: str | None,
        expected_price_paid: Decimal | None,
        expected_discount: Decimal | None,
    ) -> None:
        """Offline tickets are PENDING and record what the buyer owes."""
        code = None if code_fixture is None else t.cast(DiscountCode, request.getfixturevalue(code_fixture)).code
        response = _checkout(buyer_client, parity_event, offline_tier, code=code)

        assert response.status_code == 200, response.content
        assert response.json()["requires_payment"] is False
        tickets = list(Ticket.objects.filter(tier=offline_tier))
        assert len(tickets) == 2
        _assert_tickets(
            tickets,
            status=Ticket.TicketStatus.PENDING,
            price_paid=expected_price_paid,
            discount_amount=expected_discount,
        )
        assert not Payment.objects.filter(ticket__tier=offline_tier).exists()

    @pytest.mark.parametrize(
        "code_fixture,expected_price_paid,expected_discount",
        [
            (None, None, None),
            ("pct20", Decimal("20.00"), Decimal("5.00")),
            ("fix10", Decimal("15.00"), Decimal("10.00")),
        ],
    )
    def test_at_the_door(
        self,
        request: pytest.FixtureRequest,
        buyer_client: Client,
        parity_event: Event,
        at_the_door_tier: TicketTier,
        code_fixture: str | None,
        expected_price_paid: Decimal | None,
        expected_discount: Decimal | None,
    ) -> None:
        """At-the-door tickets go ACTIVE immediately, price recorded for collection."""
        code = None if code_fixture is None else t.cast(DiscountCode, request.getfixturevalue(code_fixture)).code
        response = _checkout(buyer_client, parity_event, at_the_door_tier, code=code)

        assert response.status_code == 200, response.content
        tickets = list(Ticket.objects.filter(tier=at_the_door_tier))
        assert len(tickets) == 2
        _assert_tickets(
            tickets,
            status=Ticket.TicketStatus.ACTIVE,
            price_paid=expected_price_paid,
            discount_amount=expected_discount,
        )

    def test_free_tier_without_code(self, buyer_client: Client, parity_event: Event, free_tier: TicketTier) -> None:
        """A free tier stamps neither a price nor a discount."""
        response = _checkout(buyer_client, parity_event, free_tier, code=None)

        assert response.status_code == 200, response.content
        tickets = list(Ticket.objects.filter(tier=free_tier))
        assert len(tickets) == 2
        _assert_tickets(
            tickets,
            status=Ticket.TicketStatus.ACTIVE,
            price_paid=None,
            discount_amount=None,
        )

    def test_free_tier_rejects_discount_codes(
        self, buyer_client: Client, parity_event: Event, free_tier: TicketTier, pct20: DiscountCode
    ) -> None:
        """Discount codes on free tiers are refused upstream — no tickets created."""
        response = _checkout(buyer_client, parity_event, free_tier, code=pct20.code)

        assert response.status_code == 400
        assert not Ticket.objects.filter(tier=free_tier).exists()


# ---------------------------------------------------------------------------
# Guest checkout — guest.py site #1 (online, immediate reservation)
# ---------------------------------------------------------------------------


def _guest_items(count: int = 2) -> list[schema.TicketPurchaseItem]:
    return [schema.TicketPurchaseItem(guest_name=f"Guest {i}") for i in range(count)]


class TestGuestOnlineParity:
    """``handle_guest_ticket_checkout`` reserves online batches directly."""

    @pytest.mark.parametrize(
        "code_fixture,expected_amount,expected_discount",
        [
            (None, Decimal("25.00"), None),
            ("pct20", Decimal("20.00"), Decimal("5.00")),
            ("fix10", Decimal("15.00"), Decimal("10.00")),
        ],
    )
    def test_guest_online_payment_rows(
        self,
        request: pytest.FixtureRequest,
        parity_event: Event,
        online_tier: TicketTier,
        code_fixture: str | None,
        expected_amount: Decimal,
        expected_discount: Decimal | None,
    ) -> None:
        """The guest online path must price identically to the authenticated one."""
        code = None if code_fixture is None else t.cast(DiscountCode, request.getfixturevalue(code_fixture)).code
        response = handle_guest_ticket_checkout(
            parity_event,
            online_tier,
            email="guest-online@example.com",
            first_name="Gina",
            last_name="Guest",
            tickets=_guest_items(),
            discount_code=code,
        )

        assert response.requires_payment is True
        payments = list(Payment.objects.filter(reservation_id=response.reservation_id))
        assert len(payments) == 2
        assert [p.amount for p in payments] == [expected_amount, expected_amount]

        tickets = list(Ticket.objects.filter(id__in=[p.ticket_id for p in payments]))
        _assert_tickets(
            tickets,
            status=Ticket.TicketStatus.PENDING,
            price_paid=None,
            discount_amount=expected_discount,
        )

    def test_guest_online_full_discount_500s_today(
        self, parity_event: Event, online_tier: TicketTier, fix_full: DiscountCode
    ) -> None:
        """Pinning a **pre-existing** defect, so the refactor is provably neutral on it.

        A whole-ticket code makes an ONLINE batch free, so ``create_batch`` reroutes
        to the free checkout and returns a *list*. The guest ONLINE branch
        (``guest.py``) only knows how to handle the ``(tickets, reservation_id)``
        tuple and raises 500 — after the ACTIVE tickets were created and the code's
        usage counter incremented. The authenticated controller handles the same
        case correctly. Out of scope for Task 5 (unchanged before and after); worth
        fixing separately.
        """
        with pytest.raises(HttpError) as exc:
            handle_guest_ticket_checkout(
                parity_event,
                online_tier,
                email="guest-free@example.com",
                first_name="Gina",
                last_name="Guest",
                tickets=_guest_items(),
                discount_code=fix_full.code,
            )

        assert exc.value.status_code == 500
        tickets = list(Ticket.objects.filter(tier=online_tier))
        assert len(tickets) == 2
        _assert_tickets(
            tickets,
            status=Ticket.TicketStatus.ACTIVE,
            price_paid=None,
            discount_amount=Decimal("25.00"),
        )
        assert not Payment.objects.filter(ticket__tier=online_tier).exists()


# ---------------------------------------------------------------------------
# Guest checkout — guest.py site #2 (token confirmation, non-online tiers)
# ---------------------------------------------------------------------------


class TestGuestConfirmParity:
    """``confirm_guest_action`` re-validates the code and creates the tickets."""

    @pytest.mark.parametrize(
        "code_fixture,expected_price_paid,expected_discount",
        [
            (None, None, None),
            ("pct20", Decimal("20.00"), Decimal("5.00")),
            ("fix10", Decimal("15.00"), Decimal("10.00")),
            ("fix_full", Decimal("0.00"), Decimal("25.00")),
        ],
    )
    def test_guest_offline_confirmation(
        self,
        request: pytest.FixtureRequest,
        parity_event: Event,
        offline_tier: TicketTier,
        code_fixture: str | None,
        expected_price_paid: Decimal | None,
        expected_discount: Decimal | None,
    ) -> None:
        """The second guest call site must price identically to the first."""
        code = None if code_fixture is None else t.cast(DiscountCode, request.getfixturevalue(code_fixture)).code
        checkout = handle_guest_ticket_checkout(
            parity_event,
            offline_tier,
            email="guest-offline@example.com",
            first_name="Gino",
            last_name="Guest",
            tickets=_guest_items(),
            discount_code=code,
        )
        assert checkout.message is not None
        assert not Ticket.objects.filter(tier=offline_tier).exists()

        # Re-derive the token the confirmation email carried (Celery is not run here).
        from events.service.guest import create_guest_ticket_token

        token = create_guest_ticket_token(
            RevelUser.objects.get(email="guest-offline@example.com"),
            parity_event.id,
            offline_tier.id,
            _guest_items(),
            None,
            code,
        )

        result = confirm_guest_action(token)
        assert isinstance(result, schema.BatchCheckoutResponse)
        assert len(result.tickets) == 2

        tickets = list(Ticket.objects.filter(tier=offline_tier))
        assert len(tickets) == 2
        _assert_tickets(
            tickets,
            status=Ticket.TicketStatus.PENDING,
            price_paid=expected_price_paid,
            discount_amount=expected_discount,
        )

    @pytest.mark.parametrize(
        "code_fixture,expected_price_paid,expected_discount",
        [
            (None, None, None),
            ("pct20", Decimal("20.00"), Decimal("5.00")),
        ],
    )
    def test_guest_at_the_door_confirmation(
        self,
        request: pytest.FixtureRequest,
        parity_event: Event,
        at_the_door_tier: TicketTier,
        code_fixture: str | None,
        expected_price_paid: Decimal | None,
        expected_discount: Decimal | None,
    ) -> None:
        """At-the-door guest confirmation records the discounted price on ACTIVE tickets."""
        from events.service.guest import create_guest_ticket_token, get_or_create_guest_user

        code = None if code_fixture is None else t.cast(DiscountCode, request.getfixturevalue(code_fixture)).code
        user = get_or_create_guest_user("guest-door@example.com", "Gino", "Door")
        token = create_guest_ticket_token(user, parity_event.id, at_the_door_tier.id, _guest_items(), None, code)

        result = confirm_guest_action(token)
        assert isinstance(result, schema.BatchCheckoutResponse)

        tickets = list(Ticket.objects.filter(tier=at_the_door_tier))
        assert len(tickets) == 2
        _assert_tickets(
            tickets,
            status=Ticket.TicketStatus.ACTIVE,
            price_paid=expected_price_paid,
            discount_amount=expected_discount,
        )

    def test_guest_pwyc_amount_still_reaches_price_paid(self, parity_event: Event, offline_tier: TicketTier) -> None:
        """PWYC is the *only* remaining meaning of the scalar — it must survive the narrowing."""
        from events.service.guest import create_guest_ticket_token, get_or_create_guest_user

        offline_tier.price_type = TicketTier.PriceType.PWYC
        offline_tier.pwyc_min = Decimal("5.00")
        offline_tier.pwyc_max = Decimal("100.00")
        offline_tier.save()

        user = get_or_create_guest_user("guest-pwyc@example.com", "Pat", "Wyc")
        token = create_guest_ticket_token(
            user, parity_event.id, offline_tier.id, _guest_items(), Decimal("17.30"), None
        )

        result = confirm_guest_action(token)
        assert isinstance(result, schema.BatchCheckoutResponse)

        tickets = list(Ticket.objects.filter(tier=offline_tier))
        assert len(tickets) == 2
        _assert_tickets(
            tickets,
            status=Ticket.TicketStatus.PENDING,
            price_paid=Decimal("17.30"),
            discount_amount=None,
        )
