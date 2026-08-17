"""Tests for POST /events/{event_id}/checkout/public — the guest multi-tier cart endpoint (#846 Task 10).

Mirrors ``test_multi_tier_checkout.py`` (the authenticated route from Task 9) for the
unauthenticated guest flow, plus the JWT-specific concerns: the non-online path mints
a grouped confirmation token instead of writing tickets immediately, and a legacy
(pre-#846) flat-shape token must still confirm correctly.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

import pytest
from django.conf import settings
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.jwt import create_token
from accounts.models import RevelUser
from events import schema
from events.models import Event, Organization, Ticket, TicketTier
from events.models.discount_code import DiscountCode

pytestmark = pytest.mark.django_db


def _tier(
    event: Event,
    name: str,
    *,
    method: TicketTier.PaymentMethod = TicketTier.PaymentMethod.FREE,
    price: Decimal = Decimal("0.00"),
    currency: str = "EUR",
    price_type: TicketTier.PriceType = TicketTier.PriceType.FIXED,
    **kwargs: object,
) -> TicketTier:
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


@pytest.fixture
def guest_event(organization: Organization, next_week: datetime) -> Event:
    """A public event that allows guest (unauthenticated) checkout."""
    return Event.objects.create(
        organization=organization,
        name="Guest Multi-Tier Event",
        slug="guest-multi-tier-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        status=Event.EventStatus.OPEN,
        start=next_week,
        end=next_week + timedelta(days=1),
        max_attendees=100,
        can_attend_without_login=True,
        requires_ticket=True,
    )


@pytest.fixture
def login_required_event(organization: Organization, next_week: datetime) -> Event:
    """Same as ``guest_event`` but does NOT allow guest access."""
    return Event.objects.create(
        organization=organization,
        name="Login Required Multi-Tier Event",
        slug="login-required-multi-tier-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        status=Event.EventStatus.OPEN,
        start=next_week,
        end=next_week + timedelta(days=1),
        max_attendees=100,
        can_attend_without_login=False,
        requires_ticket=True,
    )


def _url(event: Event) -> str:
    return reverse("api:guest_multi_tier_checkout", kwargs={"event_id": event.pk})


class TestGuestFreeOfflineCart:
    """Non-online cart: a confirmation email is sent, tickets are created on confirm."""

    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    @pytest.mark.django_db(transaction=True)
    def test_free_two_tier_cart_sends_confirmation_then_confirms_to_tickets(
        self, mock_send_email: Mock, guest_event: Event
    ) -> None:
        """Uses ``transaction=True`` because the non-online checkout branch schedules
        ``send_guest_ticket_confirmation`` via ``transaction.on_commit``. In default
        pytest-django mode the wrapping transaction is rolled back and the callback
        never fires, breaking the ``mock_send_email`` assertion.
        """
        guest_event.max_tickets_per_user = None
        guest_event.save(update_fields=["max_tickets_per_user"])
        tier_a = _tier(guest_event, "Free A")
        tier_b = _tier(guest_event, "Free B")
        payload = {
            "email": "guestcart@example.com",
            "first_name": "Guest",
            "last_name": "Cart",
            "items": [
                {"tier_id": str(tier_a.id), "tickets": [{"guest_name": "A1"}, {"guest_name": "A2"}]},
                {"tier_id": str(tier_b.id), "tickets": [{"guest_name": "B1"}]},
            ],
        }
        client = Client()

        response = client.post(_url(guest_event), data=payload, content_type="application/json")

        assert response.status_code == 200, response.content
        data = response.json()
        assert data["message"]
        assert data["tickets"] == []
        assert data["requires_payment"] is False

        guest_user = RevelUser.objects.get(email="guestcart@example.com")
        assert guest_user.guest is True
        assert not Ticket.objects.filter(user=guest_user, event=guest_event).exists()

        mock_send_email.assert_called_once()
        token = mock_send_email.call_args[0][1]

        confirm_url = reverse("api:confirm_guest_action")
        confirm_response = client.post(confirm_url, data={"token": token}, content_type="application/json")

        assert confirm_response.status_code == 200, confirm_response.content
        confirm_data = confirm_response.json()
        assert len(confirm_data["tickets"]) == 3
        assert {t["tier"]["name"] for t in confirm_data["tickets"]} == {"Free A", "Free B"}
        assert Ticket.objects.filter(user=guest_user, event=guest_event).count() == 3

    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    @pytest.mark.django_db(transaction=True)
    def test_require_ticket_names_400s_before_sending_email(self, mock_send_email: Mock, guest_event: Event) -> None:
        """Multi-group cart: a missing name 400s up front, never reaching the email send.

        Uses ``transaction=True`` because the non-online checkout branch schedules
        ``send_guest_ticket_confirmation`` via ``transaction.on_commit``. In default
        pytest-django mode the wrapping transaction is rolled back and on_commit
        callbacks never fire regardless, so ``mock_send_email.assert_not_called()``
        would pass vacuously; the marker makes the assertion actually meaningful.
        """
        guest_event.require_ticket_names = True
        guest_event.save(update_fields=["require_ticket_names"])
        tier_a = _tier(guest_event, "Free A")
        tier_b = _tier(guest_event, "Free B")
        payload = {
            "email": "noname@example.com",
            "items": [
                {"tier_id": str(tier_a.id), "tickets": [{"guest_name": "A1"}]},
                {"tier_id": str(tier_b.id), "tickets": [{}]},
            ],
        }
        client = Client()

        response = client.post(_url(guest_event), data=payload, content_type="application/json")

        assert response.status_code == 400, response.content
        mock_send_email.assert_not_called()


class TestGuestCartShapeValidation:
    """Cart-shape errors (``validate_cart_shape``) must 400 before the email is queued.

    Without this, a malformed non-online cart 200s "check your email" and the buyer
    hits a dead 400 on the confirm link — the exact outcome the pre-branch check
    exists to prevent (#846 review fix).
    """

    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_pwyc_tier_missing_pwyc_amount_is_400_no_email(self, mock_send_email: Mock, guest_event: Event) -> None:
        pwyc_tier = _tier(
            guest_event,
            "PWYC",
            method=TicketTier.PaymentMethod.OFFLINE,
            price_type=TicketTier.PriceType.PWYC,
            pwyc_min=Decimal("5.00"),
            pwyc_max=Decimal("50.00"),
        )
        payload = {
            "email": "pwycnoamount@example.com",
            "items": [{"tier_id": str(pwyc_tier.id), "tickets": [{"guest_name": "A"}]}],
        }
        client = Client()

        response = client.post(_url(guest_event), data=payload, content_type="application/json")

        assert response.status_code == 400, response.content
        assert response.json()["detail"] == "This tier requires a pay-what-you-can amount."
        mock_send_email.assert_not_called()

    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_mixed_currency_cart_is_400_no_email(self, mock_send_email: Mock, guest_event: Event) -> None:
        tier_a = _tier(guest_event, "Tier A", method=TicketTier.PaymentMethod.OFFLINE, price=Decimal("10.00"))
        tier_b = _tier(
            guest_event, "Tier B", method=TicketTier.PaymentMethod.OFFLINE, price=Decimal("10.00"), currency="USD"
        )
        payload = {
            "email": "mixedcurrency@example.com",
            "items": [
                {"tier_id": str(tier_a.id), "tickets": [{"guest_name": "A"}]},
                {"tier_id": str(tier_b.id), "tickets": [{"guest_name": "B"}]},
            ],
        }
        client = Client()

        response = client.post(_url(guest_event), data=payload, content_type="application/json")

        assert response.status_code == 400, response.content
        assert response.json()["detail"] == "All tickets in one checkout must use the same currency."
        mock_send_email.assert_not_called()


class TestGuestOnlineCart:
    def test_online_two_tier_cart_reserves_then_sessions(self, guest_event: Event) -> None:
        guest_event.max_tickets_per_user = None
        guest_event.save(update_fields=["max_tickets_per_user"])
        org = guest_event.organization
        org.stripe_account_id = "acct_guest_multitier"
        org.stripe_charges_enabled = True
        org.stripe_details_submitted = True
        org.save()
        tier_a = _tier(guest_event, "Online A", method=TicketTier.PaymentMethod.ONLINE, price=Decimal("20.00"))
        tier_b = _tier(guest_event, "Online B", method=TicketTier.PaymentMethod.ONLINE, price=Decimal("30.00"))
        payload = {
            "email": "onlineguestcart@example.com",
            "items": [
                {"tier_id": str(tier_a.id), "tickets": [{"guest_name": "A"}]},
                {"tier_id": str(tier_b.id), "tickets": [{"guest_name": "B"}]},
            ],
        }
        client = Client()

        with mock.patch("stripe.checkout.Session.create") as mock_create:
            response = client.post(_url(guest_event), data=payload, content_type="application/json")
            mock_create.assert_not_called()

        assert response.status_code == 200, response.content
        data = response.json()
        assert data["requires_payment"] is True
        assert data["tickets"] == []
        reservation_id = data["reservation_id"]
        assert UUID(reservation_id)

        fake = Mock(id="cs_test_guest_mt", url="https://checkout.stripe.com/c/cs_test_guest_mt")
        session_url = reverse("api:guest_checkout_session", kwargs={"reservation_id": reservation_id})
        with mock.patch("stripe.checkout.Session.create", return_value=fake) as mock_create:
            session_response = client.post(session_url, content_type="application/json")
            mock_create.assert_called_once()

        assert session_response.status_code == 200, session_response.content
        assert session_response.json()["checkout_url"] == fake.url


class TestGuestDiscountConfirm:
    """A multi-group v2 token carrying a discount_code re-validates and applies it
    per group on confirm (coverage gap flagged in review — previously only the
    authenticated multi-tier route and single-group guest carts were exercised)."""

    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    @pytest.mark.django_db(transaction=True)
    def test_multi_group_discount_applies_per_group_on_confirm(self, mock_send_email: Mock, guest_event: Event) -> None:
        """Uses ``transaction=True`` because the non-online checkout branch schedules
        ``send_guest_ticket_confirmation`` via ``transaction.on_commit``. In default
        pytest-django mode the wrapping transaction is rolled back and the callback
        never fires, breaking the ``mock_send_email`` assertion.
        """
        guest_event.max_tickets_per_user = None
        guest_event.save(update_fields=["max_tickets_per_user"])
        tier_a = _tier(guest_event, "Tier A", method=TicketTier.PaymentMethod.OFFLINE, price=Decimal("20.00"))
        tier_b = _tier(guest_event, "Tier B", method=TicketTier.PaymentMethod.OFFLINE, price=Decimal("30.00"))
        DiscountCode.objects.create(
            code="CART10",
            organization=guest_event.organization,
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("10.00"),
            is_active=True,
            max_uses_per_user=5,
        )
        payload = {
            "email": "discountcart@example.com",
            "items": [
                {"tier_id": str(tier_a.id), "tickets": [{"guest_name": "A"}]},
                {"tier_id": str(tier_b.id), "tickets": [{"guest_name": "B"}]},
            ],
            "discount_code": "CART10",
        }
        client = Client()

        response = client.post(_url(guest_event), data=payload, content_type="application/json")

        assert response.status_code == 200, response.content
        mock_send_email.assert_called_once()
        token = mock_send_email.call_args[0][1]

        confirm_url = reverse("api:confirm_guest_action")
        confirm_response = client.post(confirm_url, data={"token": token}, content_type="application/json")

        assert confirm_response.status_code == 200, confirm_response.content
        tickets = Ticket.objects.filter(event=guest_event, user__email="discountcart@example.com")
        assert tickets.count() == 2
        by_tier = {t.tier_id: t for t in tickets}
        assert by_tier[tier_a.id].discount_amount == Decimal("2.00")
        assert by_tier[tier_b.id].discount_amount == Decimal("3.00")


class TestLegacyTokenCompat:
    """A flat-shape token minted before #846 (no ``groups``) must still confirm."""

    def test_legacy_flat_token_confirms_to_single_tier_tickets(self, guest_event: Event) -> None:
        tier = _tier(guest_event, "Legacy Tier")
        guest_user = RevelUser.objects.create_user(
            username="legacyguest@example.com", email="legacyguest@example.com", guest=True
        )
        legacy_payload = schema.GuestTicketJWTPayloadSchema(
            user_id=guest_user.id,
            email=guest_user.email,
            event_id=guest_event.id,
            tier_id=tier.id,
            tickets=[schema.GuestTicketItemPayload(guest_name="Legacy Guest")],
            exp=timezone.now() + timedelta(hours=1),
            jti=str(uuid4()),
        )
        assert legacy_payload.groups == []
        token = create_token(legacy_payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

        client = Client()
        confirm_url = reverse("api:confirm_guest_action")
        response = client.post(confirm_url, data={"token": token}, content_type="application/json")

        assert response.status_code == 200, response.content
        data = response.json()
        assert len(data["tickets"]) == 1
        assert data["tickets"][0]["tier"]["name"] == "Legacy Tier"
        ticket = Ticket.objects.get(user=guest_user, event=guest_event)
        assert ticket.guest_name == "Legacy Guest"


class TestGuestAccessDisabled:
    def test_guest_checkout_rejects_login_required_event(self, login_required_event: Event) -> None:
        tier = _tier(login_required_event, "Tier A")
        payload = {
            "email": "blocked@example.com",
            "items": [{"tier_id": str(tier.id), "tickets": [{"guest_name": "Blocked"}]}],
        }
        client = Client()

        response = client.post(_url(login_required_event), data=payload, content_type="application/json")

        assert response.status_code == 400, response.content
        assert "login" in response.json()["detail"].lower()

    def test_login_required_gate_precedes_guest_user_creation_and_discount_validation(
        self, login_required_event: Event
    ) -> None:
        """#846 review fix: the guest-access gate must run before any guest user is
        created and before the discount code is validated. Otherwise a login-required
        event either creates a guest ``RevelUser`` row for an unvalidated request, or
        (when the email belongs to an existing non-guest account) turns "an account
        with this email already exists" into an account-existence oracle answered
        *before* the login-required 400.
        """
        tier = _tier(login_required_event, "Tier A", price=Decimal("10.00"))
        existing_email = "already-has-an-account@example.com"
        RevelUser.objects.create_user(username=existing_email, email=existing_email, guest=False)
        payload = {
            "email": existing_email,
            "items": [{"tier_id": str(tier.id), "tickets": [{"guest_name": "Blocked"}]}],
            "discount_code": "WHATEVER",
        }
        client = Client()

        response = client.post(_url(login_required_event), data=payload, content_type="application/json")

        assert response.status_code == 400, response.content
        assert "login" in response.json()["detail"].lower()


class TestAnonymousOnly:
    def test_authenticated_user_gets_400(self, member_client: Client, guest_event: Event) -> None:
        tier = _tier(guest_event, "Tier A")
        payload = {
            "email": "auth@example.com",
            "items": [{"tier_id": str(tier.id), "tickets": [{"guest_name": "Ann"}]}],
        }

        response = member_client.post(_url(guest_event), data=payload, content_type="application/json")

        assert response.status_code == 400, response.content
        assert "authenticated" in response.json()["detail"].lower()

    def test_unknown_tier_id_is_404(self, guest_event: Event) -> None:
        tier = _tier(guest_event, "Tier A")
        payload = {
            "email": "guest@example.com",
            "items": [
                {"tier_id": str(tier.id), "tickets": [{"guest_name": "Ann"}]},
                {"tier_id": str(uuid4()), "tickets": [{"guest_name": "Bob"}]},
            ],
        }
        client = Client()

        response = client.post(_url(guest_event), data=payload, content_type="application/json")

        assert response.status_code == 404, response.content
