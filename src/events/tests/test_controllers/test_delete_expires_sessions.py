"""Deleting an event, a ticket tier or an event series ends the unfinished checkouts that it cascades (#1490, #1493).

After commit, the open Stripe Checkout sessions of the cascaded PENDING payments are expired, and the
discount code uses of the cascaded PENDING tickets are given back.
"""

import typing as t
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import call, patch

import pytest
from django.db import connection
from django.db.models import F
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, EventSeries, HeldSeriesPass, Organization, Payment, SeriesPass, Ticket, TicketTier
from events.models.discount_code import DiscountCode
from events.service import event_service

pytestmark = pytest.mark.django_db

EXPIRE = "events.service.pending_checkout.Session.expire"


@pytest.fixture
def connected_org(organization: Organization) -> Organization:
    Organization.objects.filter(pk=organization.pk).update(stripe_account_id="acct_connected")
    organization.refresh_from_db()
    return organization


@pytest.fixture
def code(organization: Organization) -> DiscountCode:
    return DiscountCode.objects.create(
        code="DELCODE",
        organization=organization,
        discount_type=DiscountCode.DiscountType.PERCENTAGE,
        discount_value=Decimal("10.00"),
        max_uses=10,
        is_active=True,
    )


def _tier(event: Event, name: str = "Online") -> TicketTier:
    return TicketTier.objects.create(
        event=event, name=name, price=Decimal("10.00"), payment_method=TicketTier.PaymentMethod.ONLINE
    )


def _checkout(
    tier: TicketTier,
    user: RevelUser,
    session_id: str,
    *,
    status: str = Payment.PaymentStatus.PENDING,
    code: DiscountCode | None = None,
) -> Ticket:
    """A ticket with its Payment; a PENDING payment is an unfinished checkout, and its code use is taken."""
    pending = status == Payment.PaymentStatus.PENDING
    ticket = Ticket.objects.create(
        event=tier.event,
        tier=tier,
        user=user,
        guest_name="Holder",
        status=Ticket.TicketStatus.PENDING if pending else Ticket.TicketStatus.ACTIVE,
        discount_code=code,
    )
    Payment.objects.create(
        ticket=ticket,
        user=user,
        amount=Decimal("9.00"),
        platform_fee=Decimal("0.00"),
        currency="EUR",
        status=status,
        reservation_id=uuid.uuid4(),
        stripe_session_id=session_id,
        expires_at=timezone.now() + timedelta(hours=1),
        raw_response={},
    )
    if code is not None:
        DiscountCode.objects.filter(pk=code.pk).update(times_used=F("times_used") + 1)
    return ticket


def _used(code: DiscountCode) -> int:
    code.refresh_from_db()
    return code.times_used


def _expired_sessions(expire: t.Any) -> list[t.Any]:
    return sorted(expire.call_args_list, key=lambda c: c.args)


class TestEventDelete:
    def test_pending_sessions_are_expired_after_commit(
        self,
        organization_owner_client: Client,
        connected_org: Organization,
        event: Event,
        member_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        tier = _tier(event)
        # Two tickets of one batch share a session; it is expired once.
        _checkout(tier, member_user, "cs_batch")
        _checkout(tier, member_user, "cs_batch")
        # An issued ticket and a not-yet-sessioned checkout have no open session.
        _checkout(tier, member_user, "cs_paid", status=Payment.PaymentStatus.SUCCEEDED)
        unsessioned = _checkout(tier, member_user, "cs_unset")
        Payment.objects.filter(ticket=unsessioned).update(stripe_session_id="")
        # A checkout of another event stays open.
        other_event = Event.objects.create(organization=connected_org, name="Other", start=event.start)
        _checkout(_tier(other_event), member_user, "cs_other_event")

        url = reverse("api:delete_event", kwargs={"event_id": event.pk})
        with patch(EXPIRE) as expire:
            with django_capture_on_commit_callbacks(execute=True):
                response = organization_owner_client.delete(url)
                expire.assert_not_called()  # only after commit

        assert response.status_code == 204, response.content
        assert not Event.objects.filter(pk=event.pk).exists()
        assert expire.call_args_list == [call("cs_batch", stripe_account="acct_connected")]
        assert Payment.objects.filter(stripe_session_id="cs_other_event").exists()

    def test_failed_checkout_with_an_open_session_is_expired(
        self,
        event: Event,
        member_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """The event-cancel sweep marks a pending checkout FAILED and leaves its session open until it expires."""
        tier = _tier(event)
        swept = _checkout(tier, member_user, "cs_swept")
        Payment.objects.filter(ticket=swept).update(status=Payment.PaymentStatus.FAILED)
        Ticket.objects.filter(pk=swept.pk).update(status=Ticket.TicketStatus.CANCELLED)
        # A FAILED payment whose session already expired at Stripe needs no call.
        stale = _checkout(tier, member_user, "cs_stale")
        Payment.objects.filter(ticket=stale).update(
            status=Payment.PaymentStatus.FAILED, expires_at=timezone.now() - timedelta(minutes=1)
        )

        with patch(EXPIRE) as expire:
            with django_capture_on_commit_callbacks(execute=True):
                event_service.delete_event(event)

        assert expire.call_args_list == [call("cs_swept")]

    def test_no_callback_without_open_sessions(
        self,
        event: Event,
        member_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        _checkout(_tier(event), member_user, "cs_paid", status=Payment.PaymentStatus.SUCCEEDED)

        with patch(EXPIRE) as expire:
            with django_capture_on_commit_callbacks(execute=True) as callbacks:
                event_service.delete_event(event)

        expire.assert_not_called()
        assert not any(cb.__qualname__.endswith("expire_deleted_checkout_sessions") for cb in callbacks)

    def test_payment_rows_are_locked_first(self, event: Event, member_user: RevelUser, code: DiscountCode) -> None:
        """The session read locks only the Payment rows, before the discount release locks tiers and tickets."""
        _checkout(_tier(event), member_user, "cs_open", code=code)

        with patch(EXPIRE), CaptureQueriesContext(connection) as ctx:
            event_service.delete_event(event)

        locks = [q["sql"] for q in ctx.captured_queries if "FOR UPDATE" in q["sql"]]
        assert locks[0].split("FROM")[1].split()[0] == f'"{Payment._meta.db_table}"'
        assert f'FOR UPDATE OF "{Payment._meta.db_table}"' in locks[0]


class TestTicketTierDelete:
    def test_expires_the_tier_sessions_and_gives_back_pending_uses(
        self,
        organization_owner_client: Client,
        connected_org: Organization,
        event: Event,
        member_user: RevelUser,
        code: DiscountCode,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        tier = _tier(event)
        _checkout(tier, member_user, "cs_tier", code=code)
        # A confirmed ticket keeps its use (it is cascaded too, as before).
        _checkout(tier, member_user, "cs_tier_paid", status=Payment.PaymentStatus.SUCCEEDED, code=code)
        # A checkout of another tier of the same event stays open and keeps its use.
        _checkout(_tier(event, "Other tier"), member_user, "cs_other_tier", code=code)
        assert _used(code) == 3

        url = reverse("api:delete_ticket_tier", kwargs={"event_id": event.pk, "tier_id": tier.pk})
        with patch(EXPIRE) as expire:
            with django_capture_on_commit_callbacks(execute=True):
                response = organization_owner_client.delete(url)
                expire.assert_not_called()

        assert response.status_code == 204, response.content
        assert not TicketTier.objects.filter(pk=tier.pk).exists()
        assert expire.call_args_list == [call("cs_tier", stripe_account="acct_connected")]
        assert _used(code) == 2


class TestEventSeriesDelete:
    def test_expires_the_series_sessions_and_gives_back_pending_uses(
        self,
        organization_owner_client: Client,
        connected_org: Organization,
        event: Event,
        event_series: EventSeries,
        member_user: RevelUser,
        code: DiscountCode,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        assert event.event_series_id == event_series.pk
        second = Event.objects.create(
            organization=connected_org, name="Second", start=event.start, event_series=event_series
        )
        _checkout(_tier(event), member_user, "cs_first", code=code)
        _checkout(_tier(second), member_user, "cs_second", code=code)
        outside = Event.objects.create(organization=connected_org, name="Outside", start=event.start)
        _checkout(_tier(outside), member_user, "cs_outside", code=code)
        assert _used(code) == 3

        url = reverse("api:delete_event_series", kwargs={"series_id": event_series.pk})
        with patch(EXPIRE) as expire:
            with django_capture_on_commit_callbacks(execute=True):
                response = organization_owner_client.delete(url)
                expire.assert_not_called()

        assert response.status_code == 204, response.content
        assert not EventSeries.objects.filter(pk=event_series.pk).exists()
        assert _expired_sessions(expire) == [
            call("cs_first", stripe_account="acct_connected"),
            call("cs_second", stripe_account="acct_connected"),
        ]
        assert _used(code) == 1

    def test_a_refused_delete_expires_nothing_and_keeps_the_uses(
        self,
        organization_owner_client: Client,
        event: Event,
        event_series: EventSeries,
        member_user: RevelUser,
        code: DiscountCode,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """A held pass (even a cancelled one) protects the series: the delete rolls back with a 409."""
        _checkout(_tier(event), member_user, "cs_kept", code=code)
        series_pass = SeriesPass.objects.create(
            event_series=event_series,
            name="Season",
            price=Decimal("30.00"),
            pro_rata_discount=Decimal("0.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        HeldSeriesPass.objects.create(
            series_pass=series_pass,
            user=member_user,
            status=HeldSeriesPass.HeldSeriesPassStatus.CANCELLED,
            price_paid=series_pass.price,
        )

        url = reverse("api:delete_event_series", kwargs={"series_id": event_series.pk})
        with patch(EXPIRE) as expire:
            with django_capture_on_commit_callbacks(execute=True):
                response = organization_owner_client.delete(url)

        assert response.status_code == 409, response.content
        expire.assert_not_called()
        assert _used(code) == 1
        assert Payment.objects.filter(stripe_session_id="cs_kept", status=Payment.PaymentStatus.PENDING).exists()
