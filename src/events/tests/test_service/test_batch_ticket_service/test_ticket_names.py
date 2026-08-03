"""Ticket-name enforcement and fallback (#845)."""

from decimal import Decimal

import pytest
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, TicketTier
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service import BatchTicketService


@pytest.fixture
def free_tier(batch_event: Event) -> TicketTier:
    """Free-payment tier: ``create_batch`` returns the tickets directly, no Stripe."""
    return TicketTier.objects.create(
        event=batch_event,
        name="Free Entry",
        price=Decimal("0.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
        total_quantity=100,
    )


@pytest.fixture
def batch_service(batch_event: Event, free_tier: TicketTier, batch_user: RevelUser) -> BatchTicketService:
    """Service wired to the free tier and a fully-registered buyer."""
    return BatchTicketService(batch_event, free_tier, batch_user)


@pytest.fixture
def guest_buyer(django_user_model: type[RevelUser]) -> RevelUser:
    """Email-only guest: username == email, no first/last name."""
    return django_user_model.objects.create_user(
        username="guest@example.com",
        email="guest@example.com",
        first_name="",
        last_name="",
        guest=True,
    )


@pytest.fixture
def batch_service_guest_user(batch_event: Event, free_tier: TicketTier, guest_buyer: RevelUser) -> BatchTicketService:
    """Service wired to the free tier and an email-only guest buyer."""
    return BatchTicketService(batch_event, free_tier, guest_buyer)


@pytest.fixture
def nameless_registered_buyer(django_user_model: type[RevelUser]) -> RevelUser:
    """Self-registered account: username == email (as registration sets it), no names."""
    return django_user_model.objects.create_user(
        username="nameless@example.com",
        email="nameless@example.com",
        first_name="",
        last_name="",
    )


class TestTicketPurchaseItemNormalization:
    def test_omitted_name_is_none(self) -> None:
        assert TicketPurchaseItem().guest_name is None

    def test_empty_and_whitespace_normalize_to_none(self) -> None:
        assert TicketPurchaseItem(guest_name="").guest_name is None
        assert TicketPurchaseItem(guest_name="   ").guest_name is None

    def test_real_name_is_kept_stripped(self) -> None:
        assert TicketPurchaseItem(guest_name="  Ada Lovelace ").guest_name == "Ada Lovelace"


@pytest.mark.django_db
class TestNameEnforcement:
    def test_flag_on_missing_name_is_400(self, batch_service: BatchTicketService) -> None:
        batch_service.event.require_ticket_names = True
        batch_service.event.save(update_fields=["require_ticket_names"])
        with pytest.raises(HttpError) as exc:
            batch_service.create_batch([TicketPurchaseItem(), TicketPurchaseItem(guest_name="Named One")])
        assert exc.value.status_code == 400
        # Pin the *reason*: a bare 400 would also pass on a batch-size or capacity refusal.
        assert "name on every ticket" in str(exc.value)

    def test_flag_off_missing_name_falls_back_to_buyer_display_name(self, batch_service: BatchTicketService) -> None:
        batch_service.user.first_name = "Ada"
        batch_service.user.last_name = "Lovelace"
        batch_service.user.save(update_fields=["first_name", "last_name"])
        batch_service.event.require_ticket_names = False
        batch_service.event.save(update_fields=["require_ticket_names"])
        tickets = batch_service.create_batch([TicketPurchaseItem()])
        assert isinstance(tickets, list)
        assert tickets[0].guest_name == "Ada Lovelace"

    def test_flag_off_preferred_name_wins_over_full_name(self, batch_service: BatchTicketService) -> None:
        batch_service.user.first_name = "Ada"
        batch_service.user.last_name = "Lovelace"
        batch_service.user.preferred_name = "Countess Ada"
        batch_service.user.save(update_fields=["first_name", "last_name", "preferred_name"])
        batch_service.event.require_ticket_names = False
        batch_service.event.save(update_fields=["require_ticket_names"])
        tickets = batch_service.create_batch([TicketPurchaseItem()])
        assert isinstance(tickets, list)
        assert tickets[0].guest_name == "Countess Ada"

    def test_flag_off_explicit_name_wins(self, batch_service: BatchTicketService) -> None:
        batch_service.event.require_ticket_names = False
        batch_service.event.save(update_fields=["require_ticket_names"])
        tickets = batch_service.create_batch([TicketPurchaseItem(guest_name="Explicit Name")])
        assert isinstance(tickets, list)
        assert tickets[0].guest_name == "Explicit Name"

    def test_email_only_guest_buyer_gets_blank_name(self, batch_service_guest_user: BatchTicketService) -> None:
        svc = batch_service_guest_user
        svc.event.require_ticket_names = False
        svc.event.save(update_fields=["require_ticket_names"])
        tickets = svc.create_batch([TicketPurchaseItem()])
        assert isinstance(tickets, list)
        assert tickets[0].guest_name == ""

    def test_registered_buyer_with_blank_names_gets_blank_name_not_email(
        self, batch_event: Event, free_tier: TicketTier, nameless_registered_buyer: RevelUser
    ) -> None:
        """Registration sets username == email, so get_display_name() bottoms out at the email."""
        batch_event.require_ticket_names = False
        batch_event.save(update_fields=["require_ticket_names"])
        svc = BatchTicketService(batch_event, free_tier, nameless_registered_buyer)
        tickets = svc.create_batch([TicketPurchaseItem()])
        assert isinstance(tickets, list)
        assert tickets[0].guest_name == ""
