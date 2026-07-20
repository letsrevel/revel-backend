"""Mixed-price batch checkout: per-Payment line items and total reconciliation (#739).

Task 6 made a batch's ``Payment`` rows legitimately carry *different* amounts. The
session builder still rebuilt the uniform assumption from the database in a later
request (``effective_price = payments[0].amount``), so a 50.00 + 30.00 cart would be
charged 2x50 or 2x30 depending on unspecified row order, while the webhook marked both
Payments SUCCEEDED at 50/30 without ever comparing the session total to our books.
"""

import typing as t
from decimal import Decimal
from unittest import mock
from uuid import uuid4

import pytest
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, Organization, Payment, Ticket, TicketTier
from events.service import stripe_service
from events.service.seating.pricing import TicketPrice
from events.service.stripe_webhooks import StripeEventHandler
from events.utils.currency import to_stripe_amount

pytestmark = pytest.mark.django_db


@pytest.fixture
def stripe_connected_organization(organization: Organization) -> Organization:
    """Organization with Stripe account connected."""
    organization.stripe_account_id = "acct_test123"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.platform_fee_percent = Decimal("3.00")
    organization.platform_fee_fixed = Decimal("0.50")
    organization.save()
    return organization


@pytest.fixture
def paid_ticket_tier(event: Event, stripe_connected_organization: Organization) -> TicketTier:
    """A paid ticket tier on a Stripe-connected event."""
    event.organization = stripe_connected_organization
    event.save()
    tier = event.ticket_tiers.first()
    assert tier is not None
    tier.price = Decimal("25.00")
    tier.total_quantity = 10
    tier.save()
    return tier


def _make_ticket(event: Event, tier: TicketTier, user: RevelUser, guest_name: str) -> Ticket:
    return Ticket.objects.create(
        event=event, tier=tier, user=user, status=Ticket.TicketStatus.PENDING, guest_name=guest_name
    )


def _reserve_mixed(
    event: Event,
    tier: TicketTier,
    user: RevelUser,
    named_prices: list[tuple[str, str]],
) -> t.Any:
    """Reserve a cart of ``(guest_name, price)`` pairs and return its reservation id."""
    tickets = [_make_ticket(event, tier, user, guest_name=name) for name, _ in named_prices]
    rid = uuid4()
    stripe_service.reserve_batch_payments(
        event=event,
        tier=tier,
        user=user,
        tickets=tickets,
        reservation_id=rid,
        lines=[TicketPrice(unit_price=Decimal(price), discount_amount=Decimal("0.00")) for _, price in named_prices],
    )
    return rid


def _line_items_by_guest(line_items: list[dict[str, t.Any]]) -> dict[str, int]:
    """Map each line item's guest name to the unit amount Stripe would charge."""
    out: dict[str, int] = {}
    for item in line_items:
        description = item["price_data"]["product_data"]["description"]
        guest = description.removeprefix("Ticket for ")
        assert guest not in out, "one line item per ticket expected"
        out[guest] = item["price_data"]["unit_amount"] * item["quantity"]
    return out


class TestMixedPriceLineItems:
    """A mixed cart must bill each ticket at its own Payment row's amount."""

    def test_line_items_match_each_payment_row_amount(
        self, event: Event, paid_ticket_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        """Premium 50.00 + Standard 30.00 -> line items of 5000 and 3000, one per ticket."""
        rid = _reserve_mixed(
            event, paid_ticket_tier, organization_owner_user, [("Premium Guest", "50.00"), ("Standard Guest", "30.00")]
        )
        fake = mock.Mock(id="cs_mixed", url="https://checkout.stripe.com/c/cs_mixed")
        with mock.patch("stripe.checkout.Session.create", return_value=fake) as create:
            stripe_service.create_batch_session(reservation_id=rid)
        line_items = create.call_args.kwargs["line_items"]

        assert _line_items_by_guest(line_items) == {"Premium Guest": 5000, "Standard Guest": 3000}

    def test_session_total_equals_sum_of_payment_amounts(
        self, event: Event, paid_ticket_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        """What Stripe charges must equal what our books recorded for the batch."""
        rid = _reserve_mixed(
            event, paid_ticket_tier, organization_owner_user, [("A", "50.00"), ("B", "30.00"), ("C", "12.34")]
        )
        fake = mock.Mock(id="cs_mixed_total", url="https://checkout.stripe.com/c/cs_mixed_total")
        with mock.patch("stripe.checkout.Session.create", return_value=fake) as create:
            stripe_service.create_batch_session(reservation_id=rid)
        line_items = create.call_args.kwargs["line_items"]

        recorded = sum((p.amount for p in Payment.objects.filter(reservation_id=rid)), Decimal("0"))
        charged = sum(item["price_data"]["unit_amount"] * item["quantity"] for item in line_items)
        assert charged == to_stripe_amount(recorded, paid_ticket_tier.currency) == 9234

    def test_zero_amount_row_keeps_its_own_line_item(
        self, event: Event, paid_ticket_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        """A ticket a fixed-amount discount floored to 0.00 still gets a 0-amount line item.

        ``Payment.ticket`` is a OneToOneField, so the row exists either way; dropping its
        line item would break the 1:1 ticket<->Payment pairing the refund matcher relies on.
        """
        rid = _reserve_mixed(event, paid_ticket_tier, organization_owner_user, [("Paid", "50.00"), ("Freebie", "0.00")])
        fake = mock.Mock(id="cs_zero", url="https://checkout.stripe.com/c/cs_zero")
        with mock.patch("stripe.checkout.Session.create", return_value=fake) as create:
            stripe_service.create_batch_session(reservation_id=rid)
        line_items = create.call_args.kwargs["line_items"]

        assert _line_items_by_guest(line_items) == {"Paid": 5000, "Freebie": 0}
        assert Payment.objects.get(reservation_id=rid, ticket__guest_name="Freebie").amount == Decimal("0.00")


class TestSessionTotalReconciliation:
    """The pre-flight money invariant: never hand Stripe a total our books disagree with."""

    def test_mismatched_line_items_abort_before_stripe_is_called(
        self, event: Event, paid_ticket_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        """A builder that loses a row must raise, leaving no Stripe session and no stamp."""
        rid = _reserve_mixed(event, paid_ticket_tier, organization_owner_user, [("A", "50.00"), ("B", "30.00")])

        build = stripe_service._build_line_items

        def drop_a_row(payments: list[Payment], ev: Event, tier: TicketTier) -> list[t.Any]:
            return list(build(payments, ev, tier))[:1]

        with mock.patch.object(stripe_service, "_build_line_items", side_effect=drop_a_row):
            with mock.patch("stripe.checkout.Session.create") as create:
                with pytest.raises(stripe_service.SessionTotalMismatchError):
                    stripe_service.create_batch_session(reservation_id=rid)
                create.assert_not_called()

    def test_mismatch_maps_to_500(
        self, event: Event, paid_ticket_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        """The invariant breach surfaces as a server error, never a silent charge."""
        from events.exception_handlers import HANDLERS

        assert stripe_service.SessionTotalMismatchError in HANDLERS


class TestWebhookTotalReconciliation:
    """The confirm path must compare what Stripe charged against what we recorded."""

    @staticmethod
    def _event(session: dict[str, t.Any]) -> t.Any:
        payload = {"id": "evt_recon", "type": "checkout.session.completed", "data": {"object": session}}
        stripe_event = mock.MagicMock()
        stripe_event.__iter__.return_value = iter(payload.items())
        stripe_event.type = payload["type"]
        stripe_event.data = mock.MagicMock()
        stripe_event.data.object = session
        return stripe_event

    @pytest.fixture
    def sessioned_batch(
        self, event: Event, paid_ticket_tier: TicketTier, organization_owner_user: RevelUser
    ) -> list[Payment]:
        rid = _reserve_mixed(event, paid_ticket_tier, organization_owner_user, [("A", "50.00"), ("B", "30.00")])
        Payment.objects.filter(reservation_id=rid).update(stripe_session_id="cs_recon")
        return list(Payment.objects.filter(reservation_id=rid))

    def test_amount_total_mismatch_refuses_to_confirm(self, sessioned_batch: list[Payment]) -> None:
        """Stripe charged 100.00 but the books say 80.00 -> refuse, roll back, let Stripe retry."""
        stripe_event = self._event(
            {"id": "cs_recon", "payment_status": "paid", "payment_intent": "pi_x", "amount_total": 10000}
        )
        with pytest.raises(stripe_service.SessionTotalMismatchError):
            StripeEventHandler(stripe_event).handle_checkout_session_completed(stripe_event)
        for payment in Payment.objects.filter(stripe_session_id="cs_recon"):
            assert payment.status == Payment.PaymentStatus.PENDING

    def test_matching_amount_total_confirms(
        self, sessioned_batch: list[Payment], django_capture_on_commit_callbacks: t.Any
    ) -> None:
        """The true total (80.00 -> 8000 minor units) passes reconciliation."""
        stripe_event = self._event(
            {"id": "cs_recon", "payment_status": "paid", "payment_intent": "pi_x", "amount_total": 8000}
        )
        with mock.patch("notifications.signals.notification_requested.send"):
            with django_capture_on_commit_callbacks(execute=False):
                StripeEventHandler(stripe_event).handle_checkout_session_completed(stripe_event)
        for payment in Payment.objects.filter(stripe_session_id="cs_recon"):
            assert payment.status == Payment.PaymentStatus.SUCCEEDED

    def test_absent_amount_total_is_not_a_mismatch(
        self, sessioned_batch: list[Payment], django_capture_on_commit_callbacks: t.Any
    ) -> None:
        """Nothing to reconcile against when Stripe didn't send a total — confirm as before."""
        stripe_event = self._event({"id": "cs_recon", "payment_status": "paid", "payment_intent": "pi_x"})
        with mock.patch("notifications.signals.notification_requested.send"):
            with django_capture_on_commit_callbacks(execute=False):
                StripeEventHandler(stripe_event).handle_checkout_session_completed(stripe_event)
        for payment in Payment.objects.filter(stripe_session_id="cs_recon"):
            assert payment.status == Payment.PaymentStatus.SUCCEEDED


class TestApplicationFeeIsSummedPerRow:
    """application_fee_amount must come from the per-row platform_fee, not a scalar."""

    def test_fee_is_sum_of_row_platform_fees(
        self, event: Event, paid_ticket_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        rid = _reserve_mixed(event, paid_ticket_tier, organization_owner_user, [("A", "50.00"), ("B", "30.00")])
        payments = list(Payment.objects.filter(reservation_id=rid))
        expected = to_stripe_amount(sum((p.platform_fee for p in payments), Decimal("0")), paid_ticket_tier.currency)
        # Derived from the true 80.00 total (3% + 0.50 fixed), not 2 x the first row.
        assert expected == to_stripe_amount(Decimal("2.90"), paid_ticket_tier.currency)

        fake = mock.Mock(id="cs_fee", url="https://checkout.stripe.com/c/cs_fee")
        with mock.patch("stripe.checkout.Session.create", return_value=fake) as create:
            stripe_service.create_batch_session(reservation_id=rid)
        assert create.call_args.kwargs["payment_intent_data"]["application_fee_amount"] == expected


class TestHttpErrorStillRaisedForUnpurchasableCart:
    """An all-zero cart is still rejected at reserve — the 0-row case only exists in a mix."""

    def test_all_zero_cart_rejected(
        self, event: Event, paid_ticket_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        with pytest.raises(HttpError) as exc:
            _reserve_mixed(event, paid_ticket_tier, organization_owner_user, [("A", "0.00"), ("B", "0.00")])
        assert exc.value.status_code == 400
