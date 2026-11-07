"""Day-membership revocation, reopening and ban/resync/door-scan behaviour (#1320)."""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import stripe
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    MembershipRecord,
    MembershipTier,
    Organization,
    OrganizationMember,
    Payment,
    Ticket,
    TicketTier,
)
from events.schema import EventEditSchema
from events.service import (
    cancellation_service,
    check_in_service,
    day_membership_service,
    event_update_service,
    ticket_service,
)
from events.service.stripe_webhooks import StripeEventHandler
from events.tasks.refunds import refund_one_cancelled_event_ticket

from .conftest import records_for

pytestmark = pytest.mark.django_db

Status = Ticket.TicketStatus
Reason = MembershipRecord.EndedReason


def _only(ticket: Ticket) -> MembershipRecord:
    (record,) = records_for(ticket)
    return record


def test_user_cancel_before_start_revokes(
    day_tier_factory: t.Callable[..., TicketTier], make_ticket: t.Callable[..., Ticket], holder: RevelUser
) -> None:
    tier = day_tier_factory(allow_user_cancellation=True)
    ticket = make_ticket(tier)
    ticket = Ticket.objects.full().get(pk=ticket.pk)
    cancellation_service.cancel_ticket_by_user(ticket, holder, reason="", now=timezone.now())
    record = _only(ticket)
    assert record.ended_reason == Reason.REVOKED
    assert record.valid_from is not None  # dates untouched, row kept


def test_offline_cancel_before_start_revokes(
    day_tier_factory: t.Callable[..., TicketTier],
    make_ticket: t.Callable[..., Ticket],
    organization_owner_user: RevelUser,
) -> None:
    tier = day_tier_factory(payment_method=TicketTier.PaymentMethod.OFFLINE, price=Decimal("10.00"))
    ticket = make_ticket(tier)
    ticket_service.cancel_offline_ticket(ticket, cancelled_by=organization_owner_user)
    assert _only(ticket).ended_reason == Reason.REVOKED


def test_payment_intent_canceled_webhook_revokes(
    day_tier_factory: t.Callable[..., TicketTier], make_ticket: t.Callable[..., Ticket], holder: RevelUser
) -> None:
    tier = day_tier_factory(payment_method=TicketTier.PaymentMethod.ONLINE, price=Decimal("10.00"))
    tier.quantity_sold = 1
    tier.save(update_fields=["quantity_sold"])
    ticket = make_ticket(tier)
    Payment.objects.create(
        ticket=ticket,
        user=holder,
        stripe_session_id="cs_x",
        stripe_payment_intent_id="pi_x",
        amount=Decimal("10.00"),
        platform_fee=Decimal("0.00"),
        currency="EUR",
        status=Payment.PaymentStatus.PENDING,
        raw_response={},
    )
    obj = {"id": "pi_x"}
    data = {"type": "payment_intent.canceled", "data": {"object": obj}}
    event = MagicMock(spec=stripe.Event)
    event.__iter__.return_value = iter(data.items())
    event.type = data["type"]
    event.data = MagicMock()
    event.data.object = obj

    StripeEventHandler(event).handle_payment_intent_canceled(event)

    ticket.refresh_from_db()
    assert ticket.status == Status.CANCELLED
    assert _only(ticket).ended_reason == Reason.REVOKED


def test_unconfirm_revokes_and_reconfirm_reopens_same_record(
    day_tier_factory: t.Callable[..., TicketTier], make_ticket: t.Callable[..., Ticket]
) -> None:
    tier = day_tier_factory(payment_method=TicketTier.PaymentMethod.OFFLINE, price=Decimal("10.00"))
    ticket = make_ticket(tier)
    original = _only(ticket)

    ticket_service.unconfirm_ticket_payment(Ticket.objects.select_related("tier").get(pk=ticket.pk))
    assert _only(ticket).ended_reason == Reason.REVOKED

    ticket_service.confirm_ticket_payment(Ticket.objects.select_related("tier").get(pk=ticket.pk))
    reopened = _only(ticket)
    assert reopened.pk == original.pk
    assert reopened.ended_reason is None


def test_reopen_resets_dates_to_the_current_event_window(
    day_event: Event, day_tier: TicketTier, make_ticket: t.Callable[..., Ticket]
) -> None:
    ticket = make_ticket(day_tier)
    ticket.status = Status.CANCELLED
    ticket.save(update_fields=["status"])
    new_start = day_event.start + timedelta(days=1)
    Event.objects.filter(pk=day_event.pk).update(start=new_start, end=new_start + timedelta(hours=3))
    ticket.refresh_from_db()
    ticket.status = Status.ACTIVE
    ticket.save(update_fields=["status"])
    record = _only(ticket)
    assert record.ended_reason is None
    assert record.valid_from == new_start
    assert record.valid_until == new_start + timedelta(hours=3)


def test_bulk_event_cancellation_revokes(
    day_event: Event,
    day_tier: TicketTier,
    make_ticket: t.Callable[..., Ticket],
    organization_owner_user: RevelUser,
) -> None:
    ticket = make_ticket(day_tier)
    with patch("events.tasks.refunds.build_attendee_visibility_flags"):
        refund_one_cancelled_event_ticket(str(ticket.id), str(organization_owner_user.id))
    ticket.refresh_from_db()
    assert ticket.status == Status.CANCELLED
    assert _only(ticket).ended_reason == Reason.REVOKED


def test_no_revoke_after_start(day_event: Event, day_tier: TicketTier, make_ticket: t.Callable[..., Ticket]) -> None:
    ticket = make_ticket(day_tier)
    Event.objects.filter(pk=day_event.pk).update(start=timezone.now() - timedelta(hours=1))
    ticket = Ticket.objects.select_related("tier", "event").get(pk=ticket.pk)
    ticket.status = Status.CANCELLED
    ticket.save(update_fields=["status"])
    assert _only(ticket).ended_reason is None


def test_no_revoke_after_check_in(
    day_event: Event,
    day_tier: TicketTier,
    make_ticket: t.Callable[..., Ticket],
    organization_owner_user: RevelUser,
) -> None:
    ticket = make_ticket(day_tier)
    Event.objects.filter(pk=day_event.pk).update(
        start=timezone.now() - timedelta(minutes=5), end=timezone.now() + timedelta(hours=5)
    )
    day_event.refresh_from_db()
    check_in_service.check_in_ticket(day_event, ticket.id, organization_owner_user)
    ticket = Ticket.objects.select_related("tier", "event").get(pk=ticket.pk)
    assert ticket.checked_in_at is not None
    # There is no real path from CHECKED_IN to CANCELLED, so the hook is called directly with a CHECKED_IN->
    # CANCELLED-style transition to pin the "never revoke a checked-in holder" rule inside the service.
    # Even a status flip with a still-future event (clock moved back) must not revoke a checked-in holder.
    Event.objects.filter(pk=day_event.pk).update(start=timezone.now() + timedelta(days=1))
    ticket.event.refresh_from_db()
    day_membership_service.on_ticket_status_change(_with_status(ticket, Status.CANCELLED), Status.ACTIVE)
    assert _only(ticket).ended_reason is None


def _with_status(ticket: Ticket, status: str) -> Ticket:
    ticket.status = status
    return ticket


def test_revoke_without_record_is_a_noop(
    day_tier_factory: t.Callable[..., TicketTier], make_ticket: t.Callable[..., Ticket]
) -> None:
    ticket = make_ticket(day_tier_factory(grants=False))
    ticket.status = Status.CANCELLED
    ticket.save(update_fields=["status"])
    assert records_for(ticket) == []


# ---- ban ----


def test_ban_revokes_upcoming_keeps_ongoing_and_past(
    organization: Organization,
    day_member_tier: MembershipTier,
    holder: RevelUser,
) -> None:
    now = timezone.now()

    def rec(offset_from: timedelta, offset_until: timedelta) -> MembershipRecord:
        return MembershipRecord.objects.create(
            organization=organization,
            user=holder,
            full_name="Dana Day",
            kind=MembershipRecord.Kind.TEMPORARY,
            tier=day_member_tier,
            tier_name="Day Member",
            valid_from=now + offset_from,
            valid_until=now + offset_until,
            source=MembershipRecord.Source.TICKET,
        )

    upcoming = rec(timedelta(days=2), timedelta(days=3))
    ongoing = rec(-timedelta(hours=1), timedelta(hours=2))
    past = rec(-timedelta(days=3), -timedelta(days=2))

    OrganizationMember.objects.create(
        organization=organization, user=holder, status=OrganizationMember.MembershipStatus.BANNED
    )

    for r in (upcoming, ongoing, past):
        r.refresh_from_db()
    assert upcoming.ended_reason == Reason.REVOKED
    assert ongoing.ended_reason is None
    assert past.ended_reason is None
    assert day_membership_service.revoke_upcoming_for_ban(organization.pk, holder.pk) == 0  # idempotent


# ---- resync ----


def _edit_dates(event: Event, start_delta: timedelta) -> None:
    payload = EventEditSchema.model_construct(start=event.start + start_delta, end=event.end + start_delta)
    event_update_service.update_event(event, payload)


def test_date_edit_resyncs_non_revoked_records_only(
    day_event: Event,
    day_tier_factory: t.Callable[..., TicketTier],
    make_ticket: t.Callable[..., Ticket],
    revel_user_factory: t.Any,
) -> None:
    live = make_ticket(day_tier_factory(name="A"))
    other_user = revel_user_factory(username="b@example.com", email="b@example.com")
    revoked = make_ticket(day_tier_factory(name="B"), user=other_user)
    revoked.status = Status.CANCELLED
    revoked.save(update_fields=["status"])
    old_revoked = _only(revoked)

    _edit_dates(day_event, timedelta(days=1))

    day_event.refresh_from_db()
    assert _only(live).valid_from == day_event.start
    assert _only(live).valid_until == day_event.end
    after = _only(revoked)
    assert (after.valid_from, after.valid_until) == (old_revoked.valid_from, old_revoked.valid_until)


def test_date_edit_after_event_end_leaves_records_frozen(
    day_event: Event, day_tier: TicketTier, make_ticket: t.Callable[..., Ticket]
) -> None:
    ticket = make_ticket(day_tier)
    before = _only(ticket)
    past_end = timezone.now() - timedelta(hours=1)
    Event.objects.filter(pk=day_event.pk).update(start=past_end - timedelta(hours=6), end=past_end)
    day_event.refresh_from_db()

    _edit_dates(day_event, timedelta(days=30))

    after = _only(ticket)
    assert (after.valid_from, after.valid_until) == (before.valid_from, before.valid_until)


# ---- organizer cancel paths, ban path, deletion, bulk cancel ----


def test_admin_cancel_and_offline_refund_revoke_before_start(
    day_tier_factory: t.Callable[..., TicketTier],
    make_ticket: t.Callable[..., Ticket],
    revel_user_factory: t.Any,
    organization_owner_user: RevelUser,
) -> None:
    from events.service import refund_service

    tier = day_tier_factory(payment_method=TicketTier.PaymentMethod.OFFLINE, price=Decimal("10.00"))
    one = make_ticket(tier)
    two = make_ticket(
        day_tier_factory(payment_method=TicketTier.PaymentMethod.OFFLINE, price=Decimal("10.00")),
        user=revel_user_factory(username="o@example.com", email="o@example.com"),
    )
    refund_service.admin_cancel_ticket(one, cancelled_by=organization_owner_user, reason=None, refund_amount=None)
    ticket_service.mark_offline_ticket_refunded(two, cancelled_by=organization_owner_user)
    assert _only(one).ended_reason == Reason.REVOKED
    assert _only(two).ended_reason == Reason.REVOKED


def test_blacklisting_a_member_revokes_upcoming_day_records(
    organization: Organization,
    day_tier: TicketTier,
    make_ticket: t.Callable[..., Ticket],
    holder: RevelUser,
    organization_owner_user: RevelUser,
) -> None:
    from events.service import blacklist_service

    ticket = make_ticket(day_tier)
    OrganizationMember.objects.create(organization=organization, user=holder)
    blacklist_service.add_to_blacklist(organization, organization_owner_user, user=holder)
    assert _only(ticket).ended_reason == Reason.REVOKED


def test_ban_blocks_later_grants_and_reopens(
    organization: Organization,
    day_tier_factory: t.Callable[..., TicketTier],
    make_ticket: t.Callable[..., Ticket],
    holder: RevelUser,
) -> None:
    tier = day_tier_factory(payment_method=TicketTier.PaymentMethod.OFFLINE, price=Decimal("10.00"))
    pending = make_ticket(tier, Status.PENDING)
    granted = make_ticket(day_tier_factory(name="Other"))
    OrganizationMember.objects.create(
        organization=organization, user=holder, status=OrganizationMember.MembershipStatus.BANNED
    )
    assert _only(granted).ended_reason == Reason.REVOKED

    ticket_service.confirm_ticket_payment(Ticket.objects.select_related("tier").get(pk=pending.pk))
    assert records_for(pending) == []

    granted.refresh_from_db()
    granted.status = Status.PENDING
    granted.save(update_fields=["status"])
    granted.status = Status.ACTIVE
    granted.save(update_fields=["status"])
    assert _only(granted).ended_reason == Reason.REVOKED  # not reopened


def test_deleting_an_upcoming_event_revokes_its_records_but_not_past_ones(
    day_event: Event,
    day_tier: TicketTier,
    make_ticket: t.Callable[..., Ticket],
    organization: Organization,
    event_series: t.Any,
) -> None:
    ticket = make_ticket(day_tier)
    past_start = timezone.now() - timedelta(days=2)
    past = Event.objects.create(
        organization=organization,
        name="Past",
        slug="past",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        event_series=event_series,
        start=past_start,
        end=past_start + timedelta(hours=3),
        status=Event.EventStatus.OPEN,
        requires_ticket=True,
    )
    past_tier = TicketTier.objects.create(
        event=past,
        name="Past tier",
        price=Decimal("0.00"),
        payment_method=TicketTier.PaymentMethod.FREE,
        grants_membership_tier=day_tier.grants_membership_tier,
    )
    past_ticket = Ticket.objects.create(
        event=past, tier=past_tier, user=ticket.user, status=Status.ACTIVE, guest_name="Dana Day"
    )

    upcoming_pk = records_for_pk(ticket)
    past_pk = records_for_pk(past_ticket)
    day_event.delete()
    past.delete()

    record = MembershipRecord.objects.get(pk=upcoming_pk)
    assert record.ended_reason == Reason.REVOKED
    assert record.event_id is None  # survives deletion
    assert MembershipRecord.objects.get(pk=past_pk).ended_reason is None


def records_for_pk(ticket: Ticket) -> t.Any:
    return MembershipRecord.objects.filter(ticket=ticket).values_list("pk", flat=True).first()


def test_event_cancellation_flag_decided_at_fan_out(
    day_event: Event,
    day_tier: TicketTier,
    make_ticket: t.Callable[..., Ticket],
    organization_owner_user: RevelUser,
) -> None:
    from events.tasks.refunds import refund_cancelled_event_tickets

    ticket = make_ticket(day_tier)
    with (
        patch("events.tasks.refunds.refund_one_cancelled_event_ticket.delay") as delay,
        patch("events.tasks.refunds.send_event_refund_summary_task.apply_async"),
    ):
        refund_cancelled_event_tickets(str(day_event.id), str(organization_owner_user.id))
    assert delay.call_args.kwargs == {"revoke_day_membership": True}

    # The subtask runs after the event started: the fan-out-time decision still revokes.
    Event.objects.filter(pk=day_event.pk).update(start=timezone.now() - timedelta(hours=1))
    with patch("events.tasks.refunds.build_attendee_visibility_flags"):
        refund_one_cancelled_event_ticket(str(ticket.id), str(organization_owner_user.id), revoke_day_membership=True)
    assert _only(ticket).ended_reason == Reason.REVOKED


def test_event_cancellation_after_start_at_fan_out_does_not_revoke(
    day_event: Event,
    day_tier: TicketTier,
    make_ticket: t.Callable[..., Ticket],
    organization_owner_user: RevelUser,
) -> None:
    ticket = make_ticket(day_tier)
    with patch("events.tasks.refunds.build_attendee_visibility_flags"):
        refund_one_cancelled_event_ticket(str(ticket.id), str(organization_owner_user.id), revoke_day_membership=False)
    assert _only(ticket).ended_reason is None


def test_resync_only_moves_ticket_sourced_records(
    day_event: Event,
    day_tier: TicketTier,
    make_ticket: t.Callable[..., Ticket],
    organization: Organization,
    holder: RevelUser,
) -> None:
    ticket = make_ticket(day_tier)
    manual = MembershipRecord.objects.create(
        organization=organization,
        user=holder,
        full_name="Dana Day",
        kind=MembershipRecord.Kind.TEMPORARY,
        valid_from=day_event.start,
        valid_until=day_event.end,
        source=MembershipRecord.Source.MANUAL,
        event=day_event,
    )
    Event.objects.filter(pk=day_event.pk).update(
        start=day_event.start + timedelta(days=2), end=day_event.end + timedelta(days=2)
    )
    day_event.refresh_from_db()
    assert day_membership_service.resync_event_window(day_event) == 1
    manual.refresh_from_db()
    assert manual.valid_from != day_event.start
    assert _only(ticket).valid_from == day_event.start
