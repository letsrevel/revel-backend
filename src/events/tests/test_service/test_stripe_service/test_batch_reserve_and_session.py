"""Tests for the reserve/create-session split of the batch Stripe checkout flow (#632)."""

from decimal import Decimal
from unittest import mock
from uuid import UUID, uuid4

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, Organization, Payment, Ticket, TicketTier
from events.schema.ticket import BuyerBillingInfoSchema
from events.service import pending_checkout, stripe_service
from events.service.attendee_vat_service import BuyerVATContext
from events.service.seating.pricing import TicketPrice
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
    ga_tier = event.ticket_tiers.first()
    assert ga_tier is not None
    ga_tier.price = Decimal("25.00")
    ga_tier.total_quantity = 10
    ga_tier.save()
    return ga_tier


def _make_ticket(event: Event, tier: TicketTier, user: RevelUser, guest_name: str = "A") -> Ticket:
    return Ticket.objects.create(
        event=event, tier=tier, user=user, status=Ticket.TicketStatus.PENDING, guest_name=guest_name
    )


class TestReserveBatchPayments:
    """reserve_batch_payments: PENDING Payment rows, no Stripe call."""

    def test_reserve_creates_pending_payments_without_stripe(
        self, event: Event, paid_ticket_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        """reserve_batch_payments makes PENDING Payments with empty session id, no Stripe call."""
        tickets = [_make_ticket(event, paid_ticket_tier, organization_owner_user)]
        rid = uuid4()
        with mock.patch("stripe.checkout.Session.create") as create:
            stripe_service.reserve_batch_payments(
                event=event,
                tier=paid_ticket_tier,
                user=organization_owner_user,
                tickets=tickets,
                reservation_id=rid,
            )
            create.assert_not_called()
        payments = list(Payment.objects.filter(reservation_id=rid))
        assert len(payments) == 1
        assert payments[0].stripe_session_id == ""
        assert payments[0].status == Payment.PaymentStatus.PENDING
        assert payments[0].expires_at > timezone.now()
        assert payments[0].reservation_id == rid

    def test_reserve_sets_hold_expiry_not_default_expiry(
        self, event: Event, paid_ticket_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        """expires_at reflects RESERVATION_HOLD_MINUTES, shorter than PAYMENT_DEFAULT_EXPIRY_MINUTES."""
        from datetime import timedelta

        from django.conf import settings

        tickets = [_make_ticket(event, paid_ticket_tier, organization_owner_user)]
        rid = uuid4()
        before = timezone.now()
        stripe_service.reserve_batch_payments(
            event=event, tier=paid_ticket_tier, user=organization_owner_user, tickets=tickets, reservation_id=rid
        )
        payment = Payment.objects.get(reservation_id=rid)
        hold_ceiling = before + timedelta(minutes=settings.RESERVATION_HOLD_MINUTES)
        default_ceiling = before + timedelta(minutes=settings.PAYMENT_DEFAULT_EXPIRY_MINUTES)
        assert payment.expires_at <= hold_ceiling + timedelta(seconds=5)
        assert payment.expires_at < default_ceiling

    def test_reserve_multiple_tickets_creates_one_payment_each(
        self, event: Event, paid_ticket_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        """A batch of N tickets produces N PENDING Payment rows sharing one reservation_id."""
        tickets = [_make_ticket(event, paid_ticket_tier, organization_owner_user, guest_name=n) for n in ["A", "B"]]
        rid = uuid4()
        stripe_service.reserve_batch_payments(
            event=event, tier=paid_ticket_tier, user=organization_owner_user, tickets=tickets, reservation_id=rid
        )
        payments = list(Payment.objects.filter(reservation_id=rid))
        assert len(payments) == 2
        assert {p.ticket_id for p in payments} == {t.id for t in tickets}

    def test_reserve_raises_400_when_not_stripe_connected(
        self, event: Event, organization_owner_user: RevelUser
    ) -> None:
        """No Stripe account on the org -> 400, before any Payment row is created."""
        tier = event.ticket_tiers.first()
        assert tier is not None
        tier.price = Decimal("25.00")
        tier.save()
        tickets = [_make_ticket(event, tier, organization_owner_user)]
        rid = uuid4()
        with pytest.raises(HttpError) as exc:
            stripe_service.reserve_batch_payments(
                event=event, tier=tier, user=organization_owner_user, tickets=tickets, reservation_id=rid
            )
        assert exc.value.status_code == 400
        assert not Payment.objects.filter(reservation_id=rid).exists()

    def test_reserve_raises_400_for_zero_price(
        self, event: Event, paid_ticket_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        """A non-purchasable (<=0) price is rejected before any Payment row is created."""
        tickets = [_make_ticket(event, paid_ticket_tier, organization_owner_user)]
        rid = uuid4()
        with pytest.raises(HttpError) as exc:
            stripe_service.reserve_batch_payments(
                event=event,
                tier=paid_ticket_tier,
                user=organization_owner_user,
                tickets=tickets,
                reservation_id=rid,
                lines=[TicketPrice(unit_price=Decimal("0.00"), discount_amount=Decimal("0.00"))],
            )
        assert exc.value.status_code == 400
        assert not Payment.objects.filter(reservation_id=rid).exists()

    def test_reserve_uses_precomputed_buyer_vat_context_without_reresolving(
        self, event: Event, paid_ticket_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        """When buyer_vat_context is passed in, VIES is not re-resolved (no network under the lock)."""
        tickets = [_make_ticket(event, paid_ticket_tier, organization_owner_user)]
        rid = uuid4()
        with mock.patch.object(stripe_service, "resolve_attendee_vat_for_reserve") as resolve:
            stripe_service.reserve_batch_payments(
                event=event,
                tier=paid_ticket_tier,
                user=organization_owner_user,
                tickets=tickets,
                reservation_id=rid,
                buyer_vat_context=BuyerVATContext(buyer_country=None, buyer_vat_validated=False),
            )
            resolve.assert_not_called()
        assert Payment.objects.filter(reservation_id=rid).exists()


class TestResolveAttendeeVatForReserve:
    """resolve_attendee_vat_for_reserve: pre-lock, price-independent VIES/country resolution."""

    def test_no_billing_info_returns_none(self) -> None:
        """No billing info -> None (no VAT context to thread through)."""
        assert stripe_service.resolve_attendee_vat_for_reserve(billing_info=None) is None

    def test_returns_price_independent_context(self) -> None:
        """Billing info with an explicit country -> context with that country; no price involved."""
        billing_info = BuyerBillingInfoSchema(billing_name="Acme Corp", vat_country_code="DE")  # type: ignore[call-arg]
        context = stripe_service.resolve_attendee_vat_for_reserve(billing_info=billing_info)
        assert context == BuyerVATContext(buyer_country="DE", buyer_vat_validated=False)


class TestCreateBatchSession:
    """create_batch_session: idempotent Stripe session creation from a prior reservation."""

    def test_create_batch_session_stamps_session_id_and_returns_url(
        self, event: Event, paid_ticket_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        tickets = [_make_ticket(event, paid_ticket_tier, organization_owner_user)]
        rid = uuid4()
        stripe_service.reserve_batch_payments(
            event=event, tier=paid_ticket_tier, user=organization_owner_user, tickets=tickets, reservation_id=rid
        )
        fake = mock.Mock(id="cs_test_123", url="https://checkout.stripe.com/c/cs_test_123")
        with mock.patch("stripe.checkout.Session.create", return_value=fake) as create:
            url = stripe_service.create_batch_session(reservation_id=rid)
            assert create.call_args.kwargs["idempotency_key"] == str(rid)
        assert url == fake.url
        for p in Payment.objects.filter(reservation_id=rid):
            assert p.stripe_session_id == "cs_test_123"

    def test_create_batch_session_passes_expected_stripe_inputs(
        self, event: Event, paid_ticket_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        """application_fee_amount and per-ticket price are reconstructed from the reserved Payment rows."""
        tickets = [_make_ticket(event, paid_ticket_tier, organization_owner_user, guest_name=n) for n in ["A", "B"]]
        rid = uuid4()
        stripe_service.reserve_batch_payments(
            event=event, tier=paid_ticket_tier, user=organization_owner_user, tickets=tickets, reservation_id=rid
        )
        payments = list(Payment.objects.filter(reservation_id=rid))
        expected_fee = to_stripe_amount(
            sum((p.platform_fee for p in payments), Decimal("0")), paid_ticket_tier.currency
        )
        expected_unit_amount = to_stripe_amount(payments[0].amount, paid_ticket_tier.currency)

        fake = mock.Mock(id="cs_test_456", url="https://checkout.stripe.com/c/cs_test_456")
        with mock.patch("stripe.checkout.Session.create", return_value=fake) as create:
            stripe_service.create_batch_session(reservation_id=rid)
            kwargs = create.call_args.kwargs
        assert kwargs["payment_intent_data"]["application_fee_amount"] == expected_fee
        for line_item in kwargs["line_items"]:
            assert line_item["price_data"]["unit_amount"] == expected_unit_amount
        assert len(kwargs["line_items"]) == 2

    def test_create_batch_session_bumps_expires_at_to_default(
        self, event: Event, paid_ticket_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        """After stamping, expires_at moves from the (short) hold window to the (longer) default."""
        from datetime import timedelta

        from django.conf import settings

        tickets = [_make_ticket(event, paid_ticket_tier, organization_owner_user)]
        rid = uuid4()
        stripe_service.reserve_batch_payments(
            event=event, tier=paid_ticket_tier, user=organization_owner_user, tickets=tickets, reservation_id=rid
        )
        hold_expiry = Payment.objects.get(reservation_id=rid).expires_at

        fake = mock.Mock(id="cs_test_789", url="https://checkout.stripe.com/c/cs_test_789")
        before = timezone.now()
        with mock.patch("stripe.checkout.Session.create", return_value=fake):
            stripe_service.create_batch_session(reservation_id=rid)
        new_expiry = Payment.objects.get(reservation_id=rid).expires_at
        assert new_expiry > hold_expiry
        assert new_expiry >= before + timedelta(minutes=settings.PAYMENT_DEFAULT_EXPIRY_MINUTES) - timedelta(seconds=5)

    def test_create_batch_session_missing_reservation_404(self, event: Event) -> None:
        with pytest.raises(HttpError) as exc:
            stripe_service.create_batch_session(reservation_id=uuid4())
        assert exc.value.status_code == 404

    def test_create_batch_session_expired_reservation_404(
        self, event: Event, paid_ticket_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        from datetime import timedelta

        tickets = [_make_ticket(event, paid_ticket_tier, organization_owner_user)]
        rid = uuid4()
        stripe_service.reserve_batch_payments(
            event=event, tier=paid_ticket_tier, user=organization_owner_user, tickets=tickets, reservation_id=rid
        )
        Payment.objects.filter(reservation_id=rid).update(expires_at=timezone.now() - timedelta(minutes=1))

        with mock.patch("stripe.checkout.Session.create") as create:
            with pytest.raises(HttpError) as exc:
                stripe_service.create_batch_session(reservation_id=rid)
            create.assert_not_called()
        assert exc.value.status_code == 404

    def test_create_batch_session_already_sessioned_returns_existing_url(
        self, event: Event, paid_ticket_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        """A second call with a stamped session id resumes instead of creating a duplicate Stripe session."""
        tickets = [_make_ticket(event, paid_ticket_tier, organization_owner_user)]
        rid = uuid4()
        stripe_service.reserve_batch_payments(
            event=event, tier=paid_ticket_tier, user=organization_owner_user, tickets=tickets, reservation_id=rid
        )
        Payment.objects.filter(reservation_id=rid).update(stripe_session_id="cs_already")

        fake_resume_url = "https://checkout.stripe.com/c/cs_already"
        with mock.patch("stripe.checkout.Session.create") as create:
            with mock.patch.object(stripe_service, "resume_pending_checkout", return_value=fake_resume_url) as resume:
                url = stripe_service.create_batch_session(reservation_id=rid)
                create.assert_not_called()
                resume.assert_called_once()
        assert url == fake_resume_url


class TestCreateBatchSessionExcludesCancelledTickets:
    """A ticket user-cancelled between reserve and session-create must not be charged (#632).

    cancel_ticket_by_user -> _finalize_cancellation flips the Ticket to CANCELLED and
    releases its tier slot, but never touches the sibling Payment — which stays PENDING.
    The session step must skip such rows: no Stripe line item, no stamp (the expiry
    sweep reclaims the orphaned Payment instead).
    """

    def test_cancelled_ticket_excluded_from_line_items_and_stamp(
        self, event: Event, paid_ticket_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        tickets = [_make_ticket(event, paid_ticket_tier, organization_owner_user, guest_name=n) for n in ["A", "B"]]
        rid = uuid4()
        stripe_service.reserve_batch_payments(
            event=event, tier=paid_ticket_tier, user=organization_owner_user, tickets=tickets, reservation_id=rid
        )
        # The state cancel_ticket_by_user leaves behind: ticket CANCELLED, Payment PENDING.
        Ticket.objects.filter(pk=tickets[0].pk).update(status=Ticket.TicketStatus.CANCELLED)
        live_payment = Payment.objects.get(reservation_id=rid, ticket=tickets[1])

        fake = mock.Mock(id="cs_live_only", url="https://checkout.stripe.com/c/cs_live_only")
        with mock.patch("stripe.checkout.Session.create", return_value=fake) as create:
            url = stripe_service.create_batch_session(reservation_id=rid)

        assert url == fake.url
        kwargs = create.call_args.kwargs
        assert len(kwargs["line_items"]) == 1
        assert kwargs["metadata"]["ticket_ids"] == str(tickets[1].id)
        expected_fee = to_stripe_amount(live_payment.platform_fee, paid_ticket_tier.currency)
        assert kwargs["payment_intent_data"]["application_fee_amount"] == expected_fee
        # The cancelled ticket's orphaned Payment is not stamped — the sweep reclaims it.
        assert Payment.objects.get(reservation_id=rid, ticket=tickets[0]).stripe_session_id == ""
        assert Payment.objects.get(pk=live_payment.pk).stripe_session_id == "cs_live_only"

    def test_all_tickets_cancelled_is_404_without_stripe_call(
        self, event: Event, paid_ticket_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        tickets = [_make_ticket(event, paid_ticket_tier, organization_owner_user)]
        rid = uuid4()
        stripe_service.reserve_batch_payments(
            event=event, tier=paid_ticket_tier, user=organization_owner_user, tickets=tickets, reservation_id=rid
        )
        Ticket.objects.filter(pk=tickets[0].pk).update(status=Ticket.TicketStatus.CANCELLED)

        with mock.patch("stripe.checkout.Session.create") as create:
            with pytest.raises(HttpError) as exc:
                stripe_service.create_batch_session(reservation_id=rid)
            create.assert_not_called()
        assert exc.value.status_code == 404


class TestCreateBatchSessionDoubleSubmit:
    """claim_reservation_hold's UPDATE serializes concurrent session-creates for one
    reservation: the loser unblocks only after the winner committed its stamp, and
    must resume the winner's session instead of re-calling Stripe (same idempotency
    key with different params -> Stripe idempotency conflict -> 500)."""

    def test_resumes_when_reservation_stamped_while_claim_blocked(
        self, event: Event, paid_ticket_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        tickets = [_make_ticket(event, paid_ticket_tier, organization_owner_user)]
        rid = uuid4()
        stripe_service.reserve_batch_payments(
            event=event, tier=paid_ticket_tier, user=organization_owner_user, tickets=tickets, reservation_id=rid
        )

        def winner_stamped(reservation_id: UUID) -> None:
            # Simulates the concurrent winner committing while our claim was blocked.
            Payment.objects.filter(reservation_id=reservation_id).update(stripe_session_id="cs_winner")

        fake_resume_url = "https://checkout.stripe.com/c/cs_winner"
        with (
            mock.patch.object(stripe_service, "claim_reservation_hold", side_effect=winner_stamped),
            mock.patch("stripe.checkout.Session.create") as create,
            mock.patch.object(stripe_service, "resume_pending_checkout", return_value=fake_resume_url) as resume,
        ):
            url = stripe_service.create_batch_session(reservation_id=rid)
            create.assert_not_called()
            resume.assert_called_once()
        assert url == fake_resume_url

    def test_partial_stamp_resumes_via_a_sessioned_payment(
        self, event: Event, paid_ticket_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        """When only part of the reservation carries a session id, the resume must be
        keyed on a payment that actually has one — not an arbitrary sibling."""
        tickets = [_make_ticket(event, paid_ticket_tier, organization_owner_user, guest_name=n) for n in ["A", "B"]]
        rid = uuid4()
        stripe_service.reserve_batch_payments(
            event=event, tier=paid_ticket_tier, user=organization_owner_user, tickets=tickets, reservation_id=rid
        )
        stamped = Payment.objects.get(reservation_id=rid, ticket=tickets[1])
        Payment.objects.filter(pk=stamped.pk).update(stripe_session_id="cs_partial")

        fake_resume_url = "https://checkout.stripe.com/c/cs_partial"
        with mock.patch("stripe.checkout.Session.create") as create:
            with mock.patch.object(stripe_service, "resume_pending_checkout", return_value=fake_resume_url) as resume:
                url = stripe_service.create_batch_session(reservation_id=rid)
                create.assert_not_called()
        assert url == fake_resume_url
        assert resume.call_args.args[0] == str(stamped.id)


class TestClaimReservationHoldExtendOnly:
    """The in-flight claim must only ever extend a hold, never shorten one (#632):
    a double-submit's claim on already-stamped rows would otherwise cut the
    45-minute payment window down to the 5-minute grace while the Stripe session
    stays payable — letting the sweep reclaim rows the buyer can still pay."""

    def test_claim_does_not_shorten_a_longer_existing_hold(
        self, event: Event, paid_ticket_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        from datetime import timedelta

        tickets = [_make_ticket(event, paid_ticket_tier, organization_owner_user)]
        rid = uuid4()
        stripe_service.reserve_batch_payments(
            event=event, tier=paid_ticket_tier, user=organization_owner_user, tickets=tickets, reservation_id=rid
        )
        long_expiry = timezone.now() + timedelta(minutes=45)
        Payment.objects.filter(reservation_id=rid).update(expires_at=long_expiry)

        pending_checkout.claim_reservation_hold(rid)

        assert Payment.objects.get(reservation_id=rid).expires_at == long_expiry

    def test_claim_extends_a_near_expiry_hold(
        self, event: Event, paid_ticket_tier: TicketTier, organization_owner_user: RevelUser
    ) -> None:
        from datetime import timedelta

        tickets = [_make_ticket(event, paid_ticket_tier, organization_owner_user)]
        rid = uuid4()
        stripe_service.reserve_batch_payments(
            event=event, tier=paid_ticket_tier, user=organization_owner_user, tickets=tickets, reservation_id=rid
        )
        Payment.objects.filter(reservation_id=rid).update(expires_at=timezone.now() + timedelta(minutes=1))

        before = timezone.now()
        pending_checkout.claim_reservation_hold(rid)

        new_expiry = Payment.objects.get(reservation_id=rid).expires_at
        assert new_expiry >= before + timedelta(minutes=4)
