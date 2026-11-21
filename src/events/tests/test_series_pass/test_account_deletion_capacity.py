"""Deleting an account gives back the slots of its unfinished checkouts only; issued tickets keep theirs (#1447)."""

import typing as t
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID

import pytest
from django.contrib.admin.sites import site
from django.test import RequestFactory
from django.utils import timezone

from accounts.admin.user import RevelUserAdmin
from accounts.models import EmailVerificationReminderTracking, RevelUser
from accounts.tasks import delete_old_inactive_accounts, delete_user_account
from events.models import DiscountCode, Event, HeldSeriesPass, Payment, SeriesPass, Ticket, TicketTier, WaitlistOffer

pytestmark = pytest.mark.django_db


def _ticket(user: RevelUser, event: Event, tier: TicketTier, status: str) -> Ticket:
    return Ticket.objects.create(event=event, tier=tier, user=user, status=status, guest_name="Holder")


def _sold(model: type[TicketTier] | type[SeriesPass], pk: UUID) -> int:
    return model.objects.get(pk=pk).quantity_sold


class TestAccountDeletionCapacity:
    def test_pending_tickets_give_back_their_slots(
        self, revel_user: RevelUser, event: Event, ticket_tier: TicketTier
    ) -> None:
        _ticket(revel_user, event, ticket_tier, Ticket.TicketStatus.PENDING)
        _ticket(revel_user, event, ticket_tier, Ticket.TicketStatus.PENDING)
        TicketTier.objects.filter(pk=ticket_tier.pk).update(quantity_sold=5)

        delete_user_account(str(revel_user.pk))

        assert _sold(TicketTier, ticket_tier.pk) == 3

    @pytest.mark.parametrize(
        "status", [Ticket.TicketStatus.ACTIVE, Ticket.TicketStatus.CHECKED_IN, Ticket.TicketStatus.CANCELLED]
    )
    def test_issued_and_cancelled_tickets_keep_the_count(
        self, revel_user: RevelUser, event: Event, ticket_tier: TicketTier, status: str
    ) -> None:
        """An issued ticket keeps its slot, so its place cannot be sold a second time."""
        _ticket(revel_user, event, ticket_tier, status)
        TicketTier.objects.filter(pk=ticket_tier.pk).update(quantity_sold=5)

        delete_user_account(str(revel_user.pk))

        assert _sold(TicketTier, ticket_tier.pk) == 5

    def test_the_decrement_is_floored_at_zero(
        self, revel_user: RevelUser, event: Event, ticket_tier: TicketTier
    ) -> None:
        _ticket(revel_user, event, ticket_tier, Ticket.TicketStatus.PENDING)
        _ticket(revel_user, event, ticket_tier, Ticket.TicketStatus.PENDING)
        TicketTier.objects.filter(pk=ticket_tier.pk).update(quantity_sold=1)

        delete_user_account(str(revel_user.pk))

        assert _sold(TicketTier, ticket_tier.pk) == 0

    def test_a_pending_held_pass_gives_back_its_pass_slot(self, revel_user: RevelUser, series_pass: SeriesPass) -> None:
        HeldSeriesPass.objects.create(
            series_pass=series_pass,
            user=revel_user,
            status=HeldSeriesPass.HeldSeriesPassStatus.PENDING,
            price_paid=series_pass.price,
        )
        SeriesPass.objects.filter(pk=series_pass.pk).update(quantity_sold=4)

        delete_user_account(str(revel_user.pk))

        assert _sold(SeriesPass, series_pass.pk) == 3

    def test_an_active_held_pass_and_its_tickets_keep_their_slots(
        self, revel_user: RevelUser, series_pass: SeriesPass, event: Event, ticket_tier: TicketTier
    ) -> None:
        held = HeldSeriesPass.objects.create(
            series_pass=series_pass,
            user=revel_user,
            status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
            price_paid=series_pass.price,
        )
        Ticket.objects.create(
            event=event,
            tier=ticket_tier,
            user=revel_user,
            held_pass=held,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name="Pass Holder",
        )
        SeriesPass.objects.filter(pk=series_pass.pk).update(quantity_sold=4)
        TicketTier.objects.filter(pk=ticket_tier.pk).update(quantity_sold=4)

        delete_user_account(str(revel_user.pk))

        assert _sold(SeriesPass, series_pass.pk) == 4
        assert _sold(TicketTier, ticket_tier.pk) == 4

    def test_other_accounts_keep_their_slots(
        self, revel_user: RevelUser, member_user: RevelUser, event: Event, ticket_tier: TicketTier
    ) -> None:
        _ticket(revel_user, event, ticket_tier, Ticket.TicketStatus.PENDING)
        other = _ticket(member_user, event, ticket_tier, Ticket.TicketStatus.PENDING)
        TicketTier.objects.filter(pk=ticket_tier.pk).update(quantity_sold=2)

        delete_user_account(str(revel_user.pk))

        assert Ticket.objects.filter(pk=other.pk).exists()

        assert _sold(TicketTier, ticket_tier.pk) == 1

    def test_a_pending_held_pass_and_its_pending_tickets_give_back_every_slot_once(
        self, revel_user: RevelUser, series_pass: SeriesPass, event: Event, ticket_tier: TicketTier
    ) -> None:
        held = HeldSeriesPass.objects.create(
            series_pass=series_pass,
            user=revel_user,
            status=HeldSeriesPass.HeldSeriesPassStatus.PENDING,
            price_paid=series_pass.price,
        )
        Ticket.objects.create(
            event=event,
            tier=ticket_tier,
            user=revel_user,
            held_pass=held,
            status=Ticket.TicketStatus.PENDING,
            guest_name="Pass Holder",
        )
        SeriesPass.objects.filter(pk=series_pass.pk).update(quantity_sold=4)
        TicketTier.objects.filter(pk=ticket_tier.pk).update(quantity_sold=4)

        delete_user_account(str(revel_user.pk))

        assert _sold(SeriesPass, series_pass.pk) == 3
        assert _sold(TicketTier, ticket_tier.pk) == 3

    def test_a_coded_online_pending_ticket_gives_back_its_slot_and_its_discount_use(
        self, revel_user: RevelUser, event: Event, ticket_tier: TicketTier
    ) -> None:
        code = DiscountCode.objects.create(
            code="DELME",
            organization=event.organization,
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            discount_value=Decimal("5.00"),
            max_uses=10,
            is_active=True,
        )
        DiscountCode.objects.filter(pk=code.pk).update(times_used=1)
        ticket = Ticket.objects.create(
            event=event,
            tier=ticket_tier,
            user=revel_user,
            status=Ticket.TicketStatus.PENDING,
            guest_name="Holder",
            discount_code=code,
        )
        Payment.objects.create(
            ticket=ticket,
            user=revel_user,
            amount=Decimal("18.00"),
            platform_fee=Decimal("0.00"),
            currency="EUR",
            status=Payment.PaymentStatus.PENDING,
            reservation_id=uuid.uuid4(),
            stripe_session_id=f"cs_del_{ticket.id}",
            expires_at=timezone.now() + timedelta(hours=1),
            raw_response={},
        )
        TicketTier.objects.filter(pk=ticket_tier.pk).update(quantity_sold=2)

        delete_user_account(str(revel_user.pk))

        code.refresh_from_db()
        assert code.times_used == 0
        assert _sold(TicketTier, ticket_tier.pk) == 1


class TestOtherAccountDeletionPaths:
    """The inactive-account sweep and the admin delete run the same erasure as the GDPR task."""

    def test_the_inactive_account_sweep_gives_back_pending_slots(
        self, revel_user: RevelUser, event: Event, ticket_tier: TicketTier
    ) -> None:
        RevelUser.objects.filter(pk=revel_user.pk).update(is_active=False, email_verified=False)
        EmailVerificationReminderTracking.objects.create(
            user=revel_user, deactivation_email_sent_at=timezone.now() - timedelta(days=61)
        )
        _ticket(revel_user, event, ticket_tier, Ticket.TicketStatus.PENDING)
        TicketTier.objects.filter(pk=ticket_tier.pk).update(quantity_sold=2)

        assert delete_old_inactive_accounts()["count"] == 1

        assert not RevelUser.objects.filter(pk=revel_user.pk).exists()
        assert _sold(TicketTier, ticket_tier.pk) == 1

    def test_the_admin_delete_queues_the_account_erasure_after_commit(
        self, revel_user: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        with patch("accounts.admin.user.delete_user_account") as task:
            with django_capture_on_commit_callbacks(execute=True):
                RevelUserAdmin(RevelUser, site).delete_model(RequestFactory().post("/"), revel_user)
                task.delay.assert_not_called()  # only after commit

        task.delay.assert_called_once_with(str(revel_user.pk))
        # Removed by the task, not the request; signing in stops at once.
        assert RevelUser.objects.get(pk=revel_user.pk).is_active is False

    def test_the_admin_bulk_delete_queues_the_erasure_for_each_account(
        self, revel_user: RevelUser, member_user: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        with patch("accounts.admin.user.delete_user_account") as task:
            with django_capture_on_commit_callbacks(execute=True):
                RevelUserAdmin(RevelUser, site).delete_queryset(
                    RequestFactory().post("/"), RevelUser.objects.filter(pk__in=[revel_user.pk, member_user.pk])
                )
                task.delay.assert_not_called()  # only after commit

        assert sorted(c.args[0] for c in task.delay.call_args_list) == sorted([str(revel_user.pk), str(member_user.pk)])
        assert not RevelUser.objects.filter(pk__in=[revel_user.pk, member_user.pk], is_active=True).exists()

    def test_one_failed_admin_dispatch_does_not_skip_the_other_accounts(
        self, revel_user: RevelUser, member_user: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        with patch("accounts.admin.user.delete_user_account") as task:
            task.delay.side_effect = [RuntimeError("broker down"), None]
            with django_capture_on_commit_callbacks(execute=True):
                RevelUserAdmin(RevelUser, site).delete_queryset(
                    RequestFactory().post("/"), RevelUser.objects.filter(pk__in=[revel_user.pk, member_user.pk])
                )

        assert task.delay.call_count == 2

    def test_an_already_erased_account_is_skipped(self) -> None:
        delete_user_account(str(uuid.uuid4()))  # a second queued run must not raise

    def test_a_pending_waitlist_offer_starts_waitlist_processing_for_its_event(
        self, revel_user: RevelUser, event: Event
    ) -> None:
        WaitlistOffer.objects.create(
            event=event, user=revel_user, expires_at=timezone.now() + timedelta(hours=1), batch_id=uuid.uuid4()
        )

        with patch("events.service.account_deletion_service.enqueue_waitlist_processing") as enqueue:
            delete_user_account(str(revel_user.pk))

        enqueue.assert_called_once_with(event.pk)
