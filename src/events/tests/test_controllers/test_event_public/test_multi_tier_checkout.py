"""Tests for POST /events/{event_id}/checkout — the multi-tier cart endpoint (#846 Task 9).

Mirrors the validation matrix already pinned at the service level
(``test_batch_ticket_service/test_cart_validation.py``) but driven through the HTTP
layer, plus the endpoint-specific concerns: tier lookup scoping (404 for an unknown
or cross-event tier id), the 20-group schema cap, and the discount-code fan-out
helper (``discount_code_service.validate_cart_discount``).
"""

import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from events.models import Event, Organization, Ticket, TicketTier
from events.models.discount_code import DiscountCode

pytestmark = pytest.mark.django_db


def _tier(
    event: Event,
    name: str,
    *,
    currency: str = "EUR",
    method: TicketTier.PaymentMethod = TicketTier.PaymentMethod.OFFLINE,
    price: Decimal = Decimal("20.00"),
    price_type: TicketTier.PriceType = TicketTier.PriceType.FIXED,
    **kwargs: object,
) -> TicketTier:
    """A plain GA tier — mirrors ``test_cart_validation.py``'s helper."""
    return TicketTier.objects.create(
        event=event,
        name=name,
        price=price,
        currency=currency,
        payment_method=method,
        price_type=price_type,
        total_quantity=100,
        **kwargs,
    )


def _url(event: Event) -> str:
    return reverse("api:multi_tier_checkout", kwargs={"event_id": event.pk})


class TestCartShapeValidation:
    """The whole-cart rules from ``BatchTicketService._validate_cart``, through HTTP."""

    def test_duplicate_tier_is_400(self, member_client: Client, public_event: Event) -> None:
        tier = _tier(public_event, "Tier A")
        payload = {
            "items": [
                {"tier_id": str(tier.id), "tickets": [{"guest_name": "Ann"}]},
                {"tier_id": str(tier.id), "tickets": [{"guest_name": "Bob"}]},
            ]
        }
        response = member_client.post(_url(public_event), data=payload, content_type="application/json")
        assert response.status_code == 400, response.content
        assert response.json()["detail"] == "Each tier may appear only once per checkout."

    def test_mixed_currency_is_400(self, member_client: Client, public_event: Event) -> None:
        tier_a = _tier(public_event, "Tier A")
        tier_b = _tier(public_event, "Tier B", currency="USD")
        payload = {
            "items": [
                {"tier_id": str(tier_a.id), "tickets": [{"guest_name": "Ann"}]},
                {"tier_id": str(tier_b.id), "tickets": [{"guest_name": "Bob"}]},
            ]
        }
        response = member_client.post(_url(public_event), data=payload, content_type="application/json")
        assert response.status_code == 400, response.content
        assert response.json()["detail"] == "All tickets in one checkout must use the same currency."

    def test_mixed_payment_method_is_400(self, member_client: Client, public_event: Event) -> None:
        tier_a = _tier(public_event, "Tier A")
        tier_b = _tier(public_event, "Tier B", method=TicketTier.PaymentMethod.AT_THE_DOOR)
        payload = {
            "items": [
                {"tier_id": str(tier_a.id), "tickets": [{"guest_name": "Ann"}]},
                {"tier_id": str(tier_b.id), "tickets": [{"guest_name": "Bob"}]},
            ]
        }
        response = member_client.post(_url(public_event), data=payload, content_type="application/json")
        assert response.status_code == 400, response.content
        assert response.json()["detail"] == "All tickets in one checkout must use the same payment method."

    def test_pwyc_missing_amount_is_400(self, member_client: Client, public_event: Event) -> None:
        pwyc_tier = _tier(
            public_event,
            "PWYC",
            price_type=TicketTier.PriceType.PWYC,
            pwyc_min=Decimal("5.00"),
            pwyc_max=Decimal("50.00"),
        )
        payload = {"items": [{"tier_id": str(pwyc_tier.id), "tickets": [{"guest_name": "Ann"}]}]}
        response = member_client.post(_url(public_event), data=payload, content_type="application/json")
        assert response.status_code == 400, response.content
        assert response.json()["detail"] == "This tier requires a pay-what-you-can amount."

    def test_pwyc_amount_on_fixed_tier_is_400(self, member_client: Client, public_event: Event) -> None:
        tier = _tier(public_event, "Tier A")
        payload = {"items": [{"tier_id": str(tier.id), "tickets": [{"guest_name": "Ann"}], "pwyc_amount": "10.00"}]}
        response = member_client.post(_url(public_event), data=payload, content_type="application/json")
        assert response.status_code == 400, response.content
        assert response.json()["detail"] == "This tier does not accept a pay-what-you-can amount."

    def test_cross_group_duplicate_seat_is_400(
        self, member_client: Client, seated_event: tuple[Event, list[object]]
    ) -> None:
        event, seats = seated_event
        seat = seats[0]
        sector = seat.sector  # type: ignore[attr-defined]
        tier_a = TicketTier.objects.create(
            event=event,
            name="UC A",
            price=Decimal("20.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.OFFLINE,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
            venue=sector.venue,
            sector=sector,
            total_quantity=100,
        )
        tier_b = TicketTier.objects.create(
            event=event,
            name="UC B",
            price=Decimal("20.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.OFFLINE,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
            venue=sector.venue,
            sector=sector,
            total_quantity=100,
        )
        payload = {
            "items": [
                {"tier_id": str(tier_a.id), "tickets": [{"seat_id": str(seat.id)}]},  # type: ignore[attr-defined]
                {"tier_id": str(tier_b.id), "tickets": [{"seat_id": str(seat.id)}]},  # type: ignore[attr-defined]
            ]
        }
        response = member_client.post(_url(event), data=payload, content_type="application/json")
        assert response.status_code == 400, response.content
        assert response.json()["detail"] == "The same seat cannot be purchased twice."

    def test_more_than_20_groups_is_422(self, member_client: Client, public_event: Event) -> None:
        items = [{"tier_id": str(uuid.uuid4()), "tickets": [{"guest_name": "A"}]} for _ in range(21)]
        response = member_client.post(_url(public_event), data={"items": items}, content_type="application/json")
        assert response.status_code == 422, response.content

    def test_more_than_50_tickets_in_one_group_is_422(self, member_client: Client, public_event: Event) -> None:
        """The per-group ticket list is bounded too — 20 groups x unbounded was the hole."""
        tier = _tier(public_event, "Tier A")
        items = [{"tier_id": str(tier.id), "tickets": [{"guest_name": "A"} for _ in range(51)]}]
        response = member_client.post(_url(public_event), data={"items": items}, content_type="application/json")
        assert response.status_code == 422, response.content


class TestTierLookupScoping:
    """The tier map is scoped to the event — an unknown or cross-event id is a 404."""

    def test_unknown_tier_id_is_404(self, member_client: Client, public_event: Event) -> None:
        tier = _tier(public_event, "Tier A")
        payload = {
            "items": [
                {"tier_id": str(tier.id), "tickets": [{"guest_name": "Ann"}]},
                {"tier_id": str(uuid.uuid4()), "tickets": [{"guest_name": "Bob"}]},
            ]
        }
        response = member_client.post(_url(public_event), data=payload, content_type="application/json")
        assert response.status_code == 404, response.content
        assert response.json()["detail"] == "One or more ticket tiers were not found."

    def test_other_event_tier_is_404(
        self, member_client: Client, public_event: Event, organization: Organization
    ) -> None:
        other_event = Event.objects.create(
            organization=organization,
            name="Other Multi-Tier Event",
            slug="other-multi-tier-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            status="open",
            start=timezone.now() + timedelta(days=7),
            requires_ticket=True,
        )
        foreign_tier = _tier(other_event, "Foreign Tier")
        tier = _tier(public_event, "Tier A")
        payload = {
            "items": [
                {"tier_id": str(tier.id), "tickets": [{"guest_name": "Ann"}]},
                {"tier_id": str(foreign_tier.id), "tickets": [{"guest_name": "Bob"}]},
            ]
        }
        response = member_client.post(_url(public_event), data=payload, content_type="application/json")
        assert response.status_code == 404, response.content
        assert response.json()["detail"] == "One or more ticket tiers were not found."


class TestDiscountNoMatch:
    """Which message a cart gets when the code qualifies for NO group (``_no_qualifying_group_error``).

    The per-group reason is surfaced whenever it is unambiguous — one group checked, or
    every group rejected for the same reason — and only a genuinely mixed set of
    reasons collapses to the generic cart message.
    """

    @staticmethod
    def _code(organization: Organization, **kwargs: object) -> DiscountCode:
        return DiscountCode.objects.create(
            code="NOPE10",
            organization=organization,
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("10.00"),
            is_active=True,
            **kwargs,
        )

    def test_single_group_surfaces_the_specific_reason(
        self, member_client: Client, public_event: Event, organization: Organization
    ) -> None:
        """One group == the deprecated single-tier route's cart: its error passes through verbatim."""
        tier = _tier(public_event, "Paid A")
        self._code(organization, valid_until=timezone.now() - timedelta(days=1))
        payload = {
            "items": [{"tier_id": str(tier.id), "tickets": [{"guest_name": "Ann"}]}],
            "discount_code": "NOPE10",
        }
        response = member_client.post(_url(public_event), data=payload, content_type="application/json")
        assert response.status_code == 400, response.content
        assert response.json()["detail"] == "This discount code has expired."

    def test_multi_group_same_reason_surfaces_the_specific_reason(
        self, member_client: Client, public_event: Event, organization: Organization
    ) -> None:
        """An expired code fails identically on every group, so the reason is unambiguous."""
        tier_a = _tier(public_event, "Paid A")
        tier_b = _tier(public_event, "Paid B")
        self._code(organization, valid_until=timezone.now() - timedelta(days=1))
        payload = {
            "items": [
                {"tier_id": str(tier_a.id), "tickets": [{"guest_name": "Ann"}]},
                {"tier_id": str(tier_b.id), "tickets": [{"guest_name": "Bob"}]},
            ],
            "discount_code": "NOPE10",
        }
        response = member_client.post(_url(public_event), data=payload, content_type="application/json")
        assert response.status_code == 400, response.content
        assert response.json()["detail"] == "This discount code has expired."

    def test_multi_group_different_reasons_is_generic(
        self, member_client: Client, public_event: Event, organization: Organization
    ) -> None:
        """PWYC group vs out-of-scope group: no single per-group reason describes the cart."""
        pwyc_tier = _tier(
            public_event,
            "PWYC",
            price_type=TicketTier.PriceType.PWYC,
            pwyc_min=Decimal("5.00"),
            pwyc_max=Decimal("50.00"),
        )
        other_tier = _tier(public_event, "Paid B")
        code = self._code(organization)
        code.tiers.add(pwyc_tier)  # scoped to the PWYC tier -> the other group is out of scope
        payload = {
            "items": [
                {"tier_id": str(pwyc_tier.id), "tickets": [{"guest_name": "Ann"}], "pwyc_amount": "10.00"},
                {"tier_id": str(other_tier.id), "tickets": [{"guest_name": "Bob"}]},
            ],
            "discount_code": "NOPE10",
        }
        response = member_client.post(_url(public_event), data=payload, content_type="application/json")
        assert response.status_code == 400, response.content
        assert response.json()["detail"] == "This discount code does not apply to any tier in your cart."

    def test_multi_group_free_tiers_share_one_reason(
        self, member_client: Client, public_event: Event, organization: Organization
    ) -> None:
        """Both groups are FREE tiers — ``validate_discount_code`` refuses free tickets
        outright with the SAME message, so that message (not the generic one) is raised."""
        tier_a = TicketTier.objects.create(
            event=public_event, name="Free A", payment_method=TicketTier.PaymentMethod.FREE
        )
        tier_b = TicketTier.objects.create(
            event=public_event, name="Free B", payment_method=TicketTier.PaymentMethod.FREE
        )
        self._code(organization)
        payload = {
            "items": [
                {"tier_id": str(tier_a.id), "tickets": [{"guest_name": "Ann"}]},
                {"tier_id": str(tier_b.id), "tickets": [{"guest_name": "Bob"}]},
            ],
            "discount_code": "NOPE10",
        }
        response = member_client.post(_url(public_event), data=payload, content_type="application/json")
        assert response.status_code == 400, response.content
        assert response.json()["detail"] == "Discount codes cannot be applied to free tickets."


class TestSaleWindow:
    def test_tier_outside_sale_window_is_403(self, member_client: Client, public_event: Event) -> None:
        tier = _tier(public_event, "Future Tier", sales_start_at=timezone.now() + timedelta(days=1))
        payload = {"items": [{"tier_id": str(tier.id), "tickets": [{"guest_name": "Ann"}]}]}
        response = member_client.post(_url(public_event), data=payload, content_type="application/json")
        assert response.status_code == 403, response.content


class TestHappyPaths:
    def test_free_two_tier_cart_returns_three_tickets(self, member_client: Client, public_event: Event) -> None:
        public_event.max_tickets_per_user = None
        public_event.save(update_fields=["max_tickets_per_user"])
        tier_a = TicketTier.objects.create(
            event=public_event, name="Free A", payment_method=TicketTier.PaymentMethod.FREE
        )
        tier_b = TicketTier.objects.create(
            event=public_event, name="Free B", payment_method=TicketTier.PaymentMethod.FREE
        )
        payload = {
            "items": [
                {"tier_id": str(tier_a.id), "tickets": [{"guest_name": "A1"}, {"guest_name": "A2"}]},
                {"tier_id": str(tier_b.id), "tickets": [{"guest_name": "B1"}]},
            ]
        }
        response = member_client.post(_url(public_event), data=payload, content_type="application/json")
        assert response.status_code == 200, response.content
        body = response.json()
        assert body["requires_payment"] is False
        assert body["reservation_id"] is None
        assert len(body["tickets"]) == 3
        assert Ticket.objects.filter(event=public_event, user__username="member_user").count() == 3

    def test_online_two_tier_cart_reserves_then_sessions(self, member_client: Client, public_event: Event) -> None:
        public_event.max_tickets_per_user = None
        public_event.save(update_fields=["max_tickets_per_user"])
        org = public_event.organization
        org.stripe_account_id = "acct_multitier_ep"
        org.stripe_charges_enabled = True
        org.stripe_details_submitted = True
        org.save()
        tier_a = TicketTier.objects.create(
            event=public_event,
            name="Online A",
            price=Decimal("20.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.ONLINE,
            total_quantity=100,
        )
        tier_b = TicketTier.objects.create(
            event=public_event,
            name="Online B",
            price=Decimal("30.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.ONLINE,
            total_quantity=100,
        )
        payload = {
            "items": [
                {"tier_id": str(tier_a.id), "tickets": [{"guest_name": "A"}]},
                {"tier_id": str(tier_b.id), "tickets": [{"guest_name": "B"}]},
            ]
        }
        with mock.patch("stripe.checkout.Session.create") as mock_create:
            response = member_client.post(_url(public_event), data=payload, content_type="application/json")
            mock_create.assert_not_called()

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["requires_payment"] is True
        assert body["tickets"] == []
        reservation_id = body["reservation_id"]
        assert reservation_id

        fake = mock.Mock(id="cs_test_mt", url="https://checkout.stripe.com/c/cs_test_mt")
        session_url = reverse("api:checkout_session", kwargs={"reservation_id": reservation_id})
        with mock.patch("stripe.checkout.Session.create", return_value=fake) as mock_create:
            session_response = member_client.post(session_url, content_type="application/json")
            mock_create.assert_called_once()

        assert session_response.status_code == 200, session_response.content
        assert session_response.json()["checkout_url"] == fake.url


class TestAnonymousAccess:
    def test_anonymous_user_gets_401(self, client: Client, public_event: Event) -> None:
        tier = _tier(public_event, "Tier A")
        payload = {"items": [{"tier_id": str(tier.id), "tickets": [{"guest_name": "Ann"}]}]}
        response = client.post(_url(public_event), data=payload, content_type="application/json")
        assert response.status_code == 401
