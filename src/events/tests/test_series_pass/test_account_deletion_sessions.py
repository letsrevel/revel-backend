"""Deleting an account expires the Stripe sessions of its unfinished checkouts after commit (#1479)."""

import typing as t
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, call, patch

import pytest
import stripe
import structlog
from django.utils import timezone

from accounts.models import RevelUser
from accounts.tasks import delete_user_account
from events.models import Event, HeldSeriesPass, Organization, Payment, SeriesPass, Ticket, TicketTier

pytestmark = pytest.mark.django_db

EXPIRE = "events.service.pending_checkout.Session.expire"


def _payment(user: RevelUser, event: Event, tier: TicketTier, session_id: str, status: str) -> Payment:
    ticket_status = (
        Ticket.TicketStatus.PENDING if status == Payment.PaymentStatus.PENDING else Ticket.TicketStatus.ACTIVE
    )
    ticket = Ticket.objects.create(event=event, tier=tier, user=user, status=ticket_status, guest_name="Holder")
    return Payment.objects.create(
        ticket=ticket,
        user=user,
        amount=Decimal("10.00"),
        platform_fee=Decimal("0.00"),
        currency="EUR",
        status=status,
        reservation_id=uuid.uuid4(),
        stripe_session_id=session_id,
        expires_at=timezone.now() + timedelta(hours=1),
        raw_response={},
    )


@pytest.fixture
def connected_org(organization: Organization) -> Organization:
    Organization.objects.filter(pk=organization.pk).update(stripe_account_id="acct_connected")
    organization.refresh_from_db()
    return organization


class TestAccountDeletionExpiresSessions:
    def test_pending_sessions_are_expired_after_commit(
        self,
        revel_user: RevelUser,
        connected_org: Organization,
        event: Event,
        ticket_tier: TicketTier,
        series_pass: SeriesPass,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        # A batch of two tickets shares one session; a held pass has its own.
        _payment(revel_user, event, ticket_tier, "cs_batch", Payment.PaymentStatus.PENDING)
        _payment(revel_user, event, ticket_tier, "cs_batch", Payment.PaymentStatus.PENDING)
        HeldSeriesPass.objects.create(
            series_pass=series_pass,
            user=revel_user,
            status=HeldSeriesPass.HeldSeriesPassStatus.PENDING,
            price_paid=series_pass.price,
            stripe_session_id="cs_pass",
        )
        # Issued and not-yet-sessioned checkouts have no open session to expire.
        _payment(revel_user, event, ticket_tier, "cs_paid", Payment.PaymentStatus.SUCCEEDED)
        unsessioned = _payment(revel_user, event, ticket_tier, "cs_unset", Payment.PaymentStatus.PENDING)
        Payment.objects.filter(pk=unsessioned.pk).update(stripe_session_id="")  # reserved, session not created yet

        with patch(EXPIRE) as expire:
            with django_capture_on_commit_callbacks(execute=True):
                delete_user_account(str(revel_user.pk))
                expire.assert_not_called()  # only after commit

        assert sorted(expire.call_args_list, key=lambda c: c.args) == [
            call("cs_batch", stripe_account="acct_connected"),
            call("cs_pass", stripe_account="acct_connected"),
        ]

    def test_nothing_is_expired_when_the_deletion_rolls_back(
        self,
        revel_user: RevelUser,
        event: Event,
        ticket_tier: TicketTier,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        _payment(revel_user, event, ticket_tier, "cs_open", Payment.PaymentStatus.PENDING)

        with (
            patch(EXPIRE) as expire,
            patch("accounts.service.gdpr.purge_user_history", side_effect=RuntimeError("boom")),
        ):
            with django_capture_on_commit_callbacks(execute=True):
                with pytest.raises(RuntimeError):
                    delete_user_account(str(revel_user.pk))

        expire.assert_not_called()
        assert Payment.objects.filter(stripe_session_id="cs_open").exists()

    def test_one_failing_expire_does_not_stop_the_others(
        self,
        revel_user: RevelUser,
        event: Event,
        ticket_tier: TicketTier,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """An already expired or completed session makes Stripe raise; it is logged, and the next one still goes."""
        user_id = revel_user.pk
        _payment(revel_user, event, ticket_tier, "cs_a", Payment.PaymentStatus.PENDING)
        _payment(revel_user, event, ticket_tier, "cs_b", Payment.PaymentStatus.PENDING)

        with (
            patch(
                EXPIRE, side_effect=[stripe.error.InvalidRequestError("already expired", None), MagicMock()]
            ) as expire,
            patch("events.service.pending_checkout.logger") as log,
        ):
            log_context: list[dict[str, t.Any]] = []
            log.warning.side_effect = lambda *args, **kwargs: log_context.append(
                structlog.contextvars.get_contextvars()
            )
            with django_capture_on_commit_callbacks(execute=True):
                delete_user_account(str(revel_user.pk))

        assert [c.args[0] for c in expire.call_args_list] == ["cs_a", "cs_b"]
        log.warning.assert_called_once()
        assert log.warning.call_args.kwargs["session_id"] == "cs_a"
        assert log_context == [{"user_id": str(user_id)}]  # the failure log carries the user id, not the email
        assert not RevelUser.objects.filter(pk=revel_user.pk).exists()

    def test_no_call_without_pending_payments(
        self,
        revel_user: RevelUser,
        event: Event,
        ticket_tier: TicketTier,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        _payment(revel_user, event, ticket_tier, "cs_paid", Payment.PaymentStatus.SUCCEEDED)

        with patch(EXPIRE) as expire:
            with django_capture_on_commit_callbacks(execute=True) as callbacks:
                delete_user_account(str(revel_user.pk))

        expire.assert_not_called()
        assert not any(cb.__qualname__.endswith("expire_account_sessions") for cb in callbacks)
