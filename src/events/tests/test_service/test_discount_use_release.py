"""Discount code uses taken by unfinished checkouts are given back.

``apply_discount`` bumps ``DiscountCode.times_used`` when a checkout reserves its PENDING
tickets. Every route that removes or cancels those tickets *while they are still PENDING*
(user cancel, expiry, sweeps, webhooks, organizer cancel of an unpaid ticket) returns the use,
so abandoned checkouts do not use up a limited code. A confirmed (ACTIVE / CHECKED_IN) ticket that is
later cancelled or refunded keeps its use, and no ticket is ever released twice.
"""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import stripe
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Event, Organization, Payment, Ticket, TicketTier
from events.models.discount_code import DiscountCode
from events.service import cancellation_service, discount_code_service, stripe_service, ticket_service
from events.service.stripe_webhooks import StripeEventHandler
from events.tasks import cleanup_expired_payments

pytestmark = pytest.mark.django_db


@pytest.fixture
def user_a(revel_user_factory: RevelUserFactory) -> RevelUser:
    return revel_user_factory()


@pytest.fixture
def user_b(revel_user_factory: RevelUserFactory) -> RevelUser:
    return revel_user_factory()


@pytest.fixture
def online_tier(event: Event) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name="Discounted online",
        price=Decimal("20.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
        price_type=TicketTier.PriceType.FIXED,
        total_quantity=100,
    )


@pytest.fixture
def offline_tier(event: Event) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name="Discounted offline",
        price=Decimal("20.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        price_type=TicketTier.PriceType.FIXED,
        total_quantity=100,
    )


@pytest.fixture
def limited_code(organization: Organization) -> DiscountCode:
    """A code with a single global use."""
    return DiscountCode.objects.create(
        code="ONEUSE",
        organization=organization,
        discount_type=DiscountCode.DiscountType.PERCENTAGE,
        discount_value=Decimal("10.00"),
        max_uses=1,
        max_uses_per_user=5,
        is_active=True,
    )


def _reserve(
    tier: TicketTier,
    user: RevelUser,
    code: DiscountCode,
    *,
    count: int = 1,
    expired: bool = False,
    with_payment: bool = True,
    reservation_id: t.Any = None,
    intent: str = "",
) -> list[Ticket]:
    """Mimic a checkout reserve: PENDING tickets (+ PENDING payments) carrying the code, then apply_discount."""
    import uuid

    reservation_id = reservation_id or uuid.uuid4()
    tickets: list[Ticket] = []
    for i in range(count):
        ticket = Ticket.objects.create(
            event=tier.event,
            tier=tier,
            user=user,
            guest_name=f"Guest {i}",
            status=Ticket.TicketStatus.PENDING,
            discount_code=code,
        )
        tickets.append(ticket)
        if with_payment:
            Payment.objects.create(
                ticket=ticket,
                user=user,
                amount=Decimal("18.00"),
                platform_fee=Decimal("0.00"),
                currency="EUR",
                status=Payment.PaymentStatus.PENDING,
                reservation_id=reservation_id,
                stripe_session_id=f"cs_disc_{ticket.id}",
                stripe_payment_intent_id=f"{intent}_{i}" if intent else None,
                expires_at=timezone.now() - timedelta(minutes=1) if expired else timezone.now() + timedelta(hours=1),
                raw_response={},
            )
    TicketTier.objects.filter(pk=tier.pk).update(quantity_sold=count)
    discount_code_service.apply_discount(code, user, count)
    return tickets


def _used(code: DiscountCode) -> int:
    code.refresh_from_db()
    return code.times_used


def _payment_of(ticket: Ticket) -> Payment:
    return Payment.objects.get(ticket=ticket)


class TestReleaseHelper:
    def test_counts_per_code_skips_none_and_floors(self, organization: Organization) -> None:
        a = DiscountCode.objects.create(
            code="A",
            organization=organization,
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("5.00"),
            times_used=3,
            is_active=True,
        )
        b = DiscountCode.objects.create(
            code="B",
            organization=organization,
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("5.00"),
            times_used=1,
            is_active=True,
        )
        discount_code_service.release_discount_uses([a.pk, a.pk, None, b.pk, b.pk, b.pk])
        assert _used(a) == 1
        assert _used(b) == 0  # floored, never negative

    def test_empty_is_noop(self) -> None:
        discount_code_service.release_discount_uses([])
        discount_code_service.release_discount_uses([None])


class TestCancelPendingCheckout:
    def test_cancel_frees_the_use_for_the_next_buyer(
        self, online_tier: TicketTier, limited_code: DiscountCode, user_a: RevelUser, user_b: RevelUser
    ) -> None:
        (ticket,) = _reserve(online_tier, user_a, limited_code)
        assert _used(limited_code) == 1
        with pytest.raises(HttpError):  # code exhausted while A's checkout is open
            discount_code_service.validate_discount_code(
                "ONEUSE", online_tier.event.organization, online_tier, user_b, 1
            )

        stripe_service.cancel_pending_checkout(str(_payment_of(ticket).pk), user_a)

        assert _used(limited_code) == 0
        discount_code_service.validate_discount_code("ONEUSE", online_tier.event.organization, online_tier, user_b, 1)
        _reserve(online_tier, user_b, limited_code)
        assert _used(limited_code) == 1

    def test_batch_of_three_releases_three(
        self, online_tier: TicketTier, organization: Organization, user_a: RevelUser
    ) -> None:
        code = DiscountCode.objects.create(
            code="THREE",
            organization=organization,
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("5.00"),
            max_uses=10,
            is_active=True,
            max_uses_per_user=10,
        )
        tickets = _reserve(online_tier, user_a, code, count=3)
        assert _used(code) == 3
        stripe_service.cancel_pending_checkout(str(_payment_of(tickets[0]).pk), user_a)
        assert _used(code) == 0


class TestExpiry:
    def test_cleanup_expired_batch_releases(
        self, online_tier: TicketTier, limited_code: DiscountCode, user_a: RevelUser
    ) -> None:
        (ticket,) = _reserve(online_tier, user_a, limited_code, expired=True)
        stripe_service._cleanup_expired_batch(_payment_of(ticket))
        assert _used(limited_code) == 0

    def test_cleanup_expired_payments_task_releases(
        self, online_tier: TicketTier, limited_code: DiscountCode, user_a: RevelUser
    ) -> None:
        _reserve(online_tier, user_a, limited_code, expired=True)
        assert cleanup_expired_payments() == 1
        assert _used(limited_code) == 0

    def test_two_routes_release_once(
        self, online_tier: TicketTier, organization: Organization, user_a: RevelUser, user_b: RevelUser
    ) -> None:
        """A ticket released by one route is not released again by another, so a sibling's use survives."""
        code = DiscountCode.objects.create(
            code="SHARED",
            organization=organization,
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("5.00"),
            max_uses=10,
            is_active=True,
            max_uses_per_user=10,
        )
        (a_ticket,) = _reserve(online_tier, user_a, code, expired=True)
        _reserve(online_tier, user_b, code)  # B's live checkout also holds a use
        assert _used(code) == 2

        # A cancels the pending ticket via the user route (releases A's use, leaves the Payment PENDING) ...
        cancellation_service._finalize_cancellation(
            Ticket.objects.select_related("tier").get(pk=a_ticket.pk),
            user_a,
            "",
            timezone.now(),
            Decimal("0.00"),
            "EUR",
        )
        assert _used(code) == 1
        # ... then the sweep reclaims the same (expired) payment: must not release a second time.
        cleanup_expired_payments()
        assert _used(code) == 1

    def test_cleanup_twice_releases_once(
        self, online_tier: TicketTier, organization: Organization, user_a: RevelUser, user_b: RevelUser
    ) -> None:
        code = DiscountCode.objects.create(
            code="TWICE",
            organization=organization,
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("5.00"),
            max_uses=10,
            is_active=True,
            max_uses_per_user=10,
        )
        (ticket,) = _reserve(online_tier, user_a, code, expired=True)
        _reserve(online_tier, user_b, code)
        payment = _payment_of(ticket)
        stripe_service._cleanup_expired_batch(payment)
        stripe_service._cleanup_expired_batch(payment)
        cleanup_expired_payments()
        assert _used(code) == 1


class TestPaymentIntentCanceledWebhook:
    def test_releases(self, online_tier: TicketTier, limited_code: DiscountCode, user_a: RevelUser) -> None:
        _reserve(online_tier, user_a, limited_code, intent="pi_disc")
        payload = {"id": "pi_disc_0"}
        data = {"type": "payment_intent.canceled", "data": {"object": payload}}
        stripe_event = MagicMock(spec=stripe.Event)
        stripe_event.__iter__.return_value = iter(data.items())
        stripe_event.type = data["type"]
        stripe_event.data = MagicMock()
        stripe_event.data.object = payload

        StripeEventHandler(stripe_event).handle_payment_intent_canceled(stripe_event)

        assert _used(limited_code) == 0


class TestUserAndOrganizerCancellation:
    def test_user_cancel_of_pending_ticket_releases(
        self, offline_tier: TicketTier, limited_code: DiscountCode, user_a: RevelUser
    ) -> None:
        (ticket,) = _reserve(offline_tier, user_a, limited_code, with_payment=False)
        cancellation_service._finalize_cancellation(
            Ticket.objects.select_related("tier").get(pk=ticket.pk),
            user_a,
            "",
            timezone.now(),
            Decimal("0.00"),
            "EUR",
        )
        assert _used(limited_code) == 0

    def test_user_cancel_of_confirmed_ticket_keeps_the_use(
        self, offline_tier: TicketTier, limited_code: DiscountCode, user_a: RevelUser
    ) -> None:
        (ticket,) = _reserve(offline_tier, user_a, limited_code, with_payment=False)
        Ticket.objects.filter(pk=ticket.pk).update(status=Ticket.TicketStatus.ACTIVE)
        cancellation_service._finalize_cancellation(
            Ticket.objects.select_related("tier").get(pk=ticket.pk),
            user_a,
            "",
            timezone.now(),
            Decimal("0.00"),
            "EUR",
        )
        assert _used(limited_code) == 1

    def test_organizer_cancel_of_pending_offline_ticket_releases(
        self,
        offline_tier: TicketTier,
        limited_code: DiscountCode,
        user_a: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        (ticket,) = _reserve(offline_tier, user_a, limited_code, with_payment=False)
        ticket_service.cancel_offline_ticket(ticket, cancelled_by=organization_owner_user)
        assert _used(limited_code) == 0

    def test_organizer_cancel_of_confirmed_offline_ticket_keeps_the_use(
        self,
        offline_tier: TicketTier,
        limited_code: DiscountCode,
        user_a: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        (ticket,) = _reserve(offline_tier, user_a, limited_code, with_payment=False)
        Ticket.objects.filter(pk=ticket.pk).update(status=Ticket.TicketStatus.ACTIVE)
        ticket_service.cancel_offline_ticket(ticket, cancelled_by=organization_owner_user)
        assert _used(limited_code) == 1


class TestFloor:
    def test_times_used_never_negative(
        self, online_tier: TicketTier, limited_code: DiscountCode, user_a: RevelUser
    ) -> None:
        (ticket,) = _reserve(online_tier, user_a, limited_code)
        DiscountCode.objects.filter(pk=limited_code.pk).update(times_used=0)  # counter drifted
        stripe_service.cancel_pending_checkout(str(_payment_of(ticket).pk), user_a)
        assert _used(limited_code) == 0


class TestEventCancellationSweep:
    def test_pending_ticket_releases_confirmed_keeps(
        self, offline_tier: TicketTier, organization: Organization, user_a: RevelUser, user_b: RevelUser
    ) -> None:
        from events.tasks.refunds import refund_one_cancelled_event_ticket

        code = DiscountCode.objects.create(
            code="EVTCXL",
            organization=organization,
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("5.00"),
            max_uses=10,
            max_uses_per_user=10,
            is_active=True,
        )
        (pending,) = _reserve(offline_tier, user_a, code, with_payment=False)
        (confirmed,) = _reserve(offline_tier, user_b, code, with_payment=False)
        Ticket.objects.filter(pk=confirmed.pk).update(status=Ticket.TicketStatus.ACTIVE)
        assert _used(code) == 2

        refund_one_cancelled_event_ticket(str(pending.pk))
        assert _used(code) == 1
        refund_one_cancelled_event_ticket(str(confirmed.pk))
        assert _used(code) == 1
