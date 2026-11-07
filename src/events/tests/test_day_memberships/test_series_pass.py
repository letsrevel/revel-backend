"""Day memberships granted by series-pass tickets (#1321 part B)."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    EventSeries,
    HeldSeriesPass,
    MembershipRecord,
    MembershipTier,
    Organization,
    SeriesPass,
    SeriesPassTierLink,
    Ticket,
    TicketTier,
)
from events.service import series_pass_service
from events.service.series_pass_purchase import SeriesPassPurchaseService
from events.tasks.series_pass import materialize_series_pass_holders

from .conftest import records_for

pytestmark = pytest.mark.django_db

Reason = MembershipRecord.EndedReason


def _link(
    organization: Organization,
    series: EventSeries,
    series_pass: SeriesPass,
    tier: MembershipTier,
    slug: str,
    start_in: timedelta,
) -> tuple[Event, SeriesPassTierLink]:
    start = timezone.now() + start_in
    event = Event.objects.create(
        organization=organization,
        name=f"Event {slug}",
        slug=slug,
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        event_series=series,
        max_attendees=100,
        start=start,
        end=start + timedelta(hours=4),
        status=Event.EventStatus.OPEN,
        requires_ticket=True,
    )
    ticket_tier = TicketTier.objects.create(
        event=event,
        name=f"Tier {slug}",
        price=Decimal("10.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
        grants_membership_tier=tier,
    )
    return event, SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=ticket_tier)


def _make_pass(
    organization: Organization, series: EventSeries, tier: MembershipTier, method: str
) -> tuple[SeriesPass, list[Event]]:
    series_pass = SeriesPass.objects.create(
        event_series=series,
        name="Season",
        price=Decimal("20.00"),
        pro_rata_discount=Decimal("0.00"),
        currency="EUR",
        payment_method=method,
    )
    events = [_link(organization, series, series_pass, tier, f"sp-{i}", timedelta(days=i + 2))[0] for i in range(2)]
    return series_pass, events


def _assert_window_records(held_pass: HeldSeriesPass, events: list[Event], tier: MembershipTier) -> None:
    for event in events:
        ticket = Ticket.objects.get(held_pass=held_pass, event=event)
        (record,) = records_for(ticket)
        assert record.kind == MembershipRecord.Kind.TEMPORARY
        assert record.source == MembershipRecord.Source.TICKET
        assert record.tier_id == tier.pk
        assert record.valid_from == event.start  # THAT event's window
        assert record.valid_until == event.end
        assert record.event_id == event.pk
        assert record.ended_reason is None


def test_free_pass_purchase_grants_one_record_per_event(
    organization: Organization,
    event_series: EventSeries,
    day_member_tier: MembershipTier,
    holder: RevelUser,
) -> None:
    series_pass, events = _make_pass(organization, event_series, day_member_tier, TicketTier.PaymentMethod.FREE)
    held_pass = SeriesPassPurchaseService(series_pass, holder).purchase()
    assert isinstance(held_pass, HeldSeriesPass)
    _assert_window_records(held_pass, events, day_member_tier)


def test_materialize_extension_grants_for_new_event(
    organization: Organization,
    event_series: EventSeries,
    day_member_tier: MembershipTier,
    holder: RevelUser,
) -> None:
    series_pass, events = _make_pass(organization, event_series, day_member_tier, TicketTier.PaymentMethod.FREE)
    held_pass = SeriesPassPurchaseService(series_pass, holder).purchase()
    assert isinstance(held_pass, HeldSeriesPass)
    new_event, _ = _link(organization, event_series, series_pass, day_member_tier, "sp-new", timedelta(days=9))

    with patch("notifications.signals.series_pass.send_series_pass_extended"):
        materialize_series_pass_holders(str(series_pass.id), [str(new_event.id)])

    _assert_window_records(held_pass, [*events, new_event], day_member_tier)


def test_offline_confirm_and_backfill_grant(
    organization: Organization,
    event_series: EventSeries,
    day_member_tier: MembershipTier,
    holder: RevelUser,
) -> None:
    series_pass, events = _make_pass(organization, event_series, day_member_tier, TicketTier.PaymentMethod.OFFLINE)
    held_pass = SeriesPassPurchaseService(series_pass, holder).purchase()
    assert isinstance(held_pass, HeldSeriesPass)
    # PENDING tickets: nothing granted yet.
    assert not MembershipRecord.objects.filter(user=holder).exists()
    # Extension while PENDING: the extension task skips the pending holder; the confirm backfills it.
    new_event, _ = _link(organization, event_series, series_pass, day_member_tier, "sp-late", timedelta(days=9))

    with patch("events.service.series_pass_service.send_series_pass_purchased"):
        series_pass_service.confirm_held_pass_payment(held_pass)

    _assert_window_records(held_pass, [*events, new_event], day_member_tier)


def test_cancel_held_pass_revokes_upcoming_only(
    organization: Organization,
    event_series: EventSeries,
    day_member_tier: MembershipTier,
    holder: RevelUser,
    organization_owner_user: RevelUser,
) -> None:
    series_pass, (first, second) = _make_pass(
        organization, event_series, day_member_tier, TicketTier.PaymentMethod.FREE
    )
    held_pass = SeriesPassPurchaseService(series_pass, holder).purchase()
    assert isinstance(held_pass, HeldSeriesPass)
    # The first event has already started: cancel_held_pass leaves its ticket (and record) alone.
    Event.objects.filter(pk=first.pk).update(start=timezone.now() - timedelta(hours=1))

    series_pass_service.cancel_held_pass(held_pass, cancelled_by=organization_owner_user)

    (started,) = records_for(Ticket.objects.get(held_pass=held_pass, event=first))
    (upcoming,) = records_for(Ticket.objects.get(held_pass=held_pass, event=second))
    assert started.ended_reason is None
    assert upcoming.ended_reason == Reason.REVOKED
