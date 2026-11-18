"""Series pass purchase, backfill and extension apply the per-event rules of a normal ticket purchase.

Per covered future event: blacklist (hard and fuzzy name match), event status, full profile, and
event-level capacity. All-or-nothing: a refused purchase writes nothing.
"""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    Blacklist,
    Event,
    EventSeries,
    HeldSeriesPass,
    Organization,
    OrganizationStaff,
    SeriesPass,
    SeriesPassTierLink,
    Ticket,
    TicketTier,
    WaitlistOffer,
)
from events.service import series_pass_service
from events.service.event_manager import Reasons, UserIsIneligibleError
from events.service.series_pass_purchase import SeriesPassPurchaseService
from events.tasks.series_pass import materialize_series_pass_holders

pytestmark = pytest.mark.django_db


def _covered(
    organization: Organization,
    series: EventSeries,
    series_pass: SeriesPass,
    slug: str,
    days: int = 7,
    **event_fields: t.Any,
) -> tuple[Event, TicketTier]:
    event = Event.objects.create(
        organization=organization,
        name=f"Event {slug}",
        slug=slug,
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        event_series=series,
        max_attendees=event_fields.pop("max_attendees", 100),
        start=timezone.now() + timedelta(days=days),
        status=event_fields.pop("status", Event.EventStatus.OPEN),
        requires_ticket=True,
        **event_fields,
    )
    tier = TicketTier.objects.create(
        event=event,
        name=f"Tier {slug}",
        price=Decimal("10.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
    )
    SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=tier)
    return event, tier


@pytest.fixture
def free_pass(organization: Organization, event_series: EventSeries) -> SeriesPass:
    return SeriesPass.objects.create(
        event_series=event_series,
        name="Season",
        price=Decimal("20.00"),
        pro_rata_discount=Decimal("0.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
    )


@pytest.fixture
def buyer(revel_user_factory: RevelUserFactory) -> RevelUser:
    return revel_user_factory(
        username="buyer@example.com", email="buyer@example.com", first_name="John", last_name="Smith"
    )


def _privileged_user(role: str, organization: Organization, revel_user_factory: RevelUserFactory) -> RevelUser:
    """An owner or a (non-member) staff user whose name matches ``John Smith``."""
    if role == "owner":
        user = organization.owner
        user.first_name, user.last_name = "John", "Smith"
        user.save()
        return user
    user = revel_user_factory(
        username="staffer@example.com", email="staffer@example.com", first_name="John", last_name="Smith"
    )
    OrganizationStaff.objects.create(organization=organization, user=user)
    return user


def _assert_nothing_written(series_pass: SeriesPass, tiers: list[TicketTier]) -> None:
    assert not HeldSeriesPass.objects.filter(series_pass=series_pass).exists()
    assert not Ticket.objects.filter(tier__in=tiers).exists()
    series_pass.refresh_from_db()
    assert series_pass.quantity_sold == 0
    for tier in tiers:
        tier.refresh_from_db()
        assert tier.quantity_sold == 0


class TestPurchaseEligibility:
    def test_fuzzy_blacklist_match_refused(
        self, organization: Organization, event_series: EventSeries, free_pass: SeriesPass, buyer: RevelUser
    ) -> None:
        _, t1 = _covered(organization, event_series, free_pass, "fz1")
        _, t2 = _covered(organization, event_series, free_pass, "fz2", days=8)
        Blacklist.objects.create(
            organization=organization, first_name="John", last_name="Smith", created_by=organization.owner
        )

        with pytest.raises(UserIsIneligibleError) as exc:
            SeriesPassPurchaseService(free_pass, buyer).purchase()

        assert exc.value.eligibility.reason_code == Reasons.VERIFICATION_REQUIRED.code
        _assert_nothing_written(free_pass, [t1, t2])

    def test_non_open_covered_event_refused(
        self, organization: Organization, event_series: EventSeries, free_pass: SeriesPass, buyer: RevelUser
    ) -> None:
        _, t1 = _covered(organization, event_series, free_pass, "st1")
        event, t2 = _covered(organization, event_series, free_pass, "st2", days=8)
        Event.objects.filter(pk=event.pk).update(status=Event.EventStatus.DRAFT)

        with pytest.raises(UserIsIneligibleError) as exc:
            SeriesPassPurchaseService(free_pass, buyer).purchase()

        assert exc.value.eligibility.reason_code == Reasons.EVENT_IS_NOT_OPEN.code
        assert exc.value.eligibility.event_id == event.id
        _assert_nothing_written(free_pass, [t1, t2])

    def test_incomplete_profile_refused_when_required(
        self, organization: Organization, event_series: EventSeries, free_pass: SeriesPass, buyer: RevelUser
    ) -> None:
        _, t1 = _covered(organization, event_series, free_pass, "pf1")
        _, t2 = _covered(organization, event_series, free_pass, "pf2", days=8, requires_full_profile=True)

        with pytest.raises(UserIsIneligibleError) as exc:
            SeriesPassPurchaseService(free_pass, buyer).purchase()

        assert exc.value.eligibility.reason_code == Reasons.REQUIRES_FULL_PROFILE.code
        assert "profile_picture" in (exc.value.eligibility.missing_profile_fields or [])
        _assert_nothing_written(free_pass, [t1, t2])

    @pytest.mark.parametrize("role", ["owner", "staff"])
    def test_owner_and_staff_exempt(
        self,
        organization: Organization,
        event_series: EventSeries,
        free_pass: SeriesPass,
        revel_user_factory: RevelUserFactory,
        role: str,
    ) -> None:
        """Mirrors PrivilegedAccessGate: exempt from blacklist-fuzzy, status and profile gates."""
        user = _privileged_user(role, organization, revel_user_factory)
        _covered(organization, event_series, free_pass, "ex1", requires_full_profile=True)
        event, _ = _covered(organization, event_series, free_pass, "ex2", days=8)
        Event.objects.filter(pk=event.pk).update(status=Event.EventStatus.DRAFT)
        Blacklist.objects.create(
            organization=organization, first_name="John", last_name="Smith", created_by=organization.owner
        )

        result = SeriesPassPurchaseService(free_pass, user).purchase()

        assert isinstance(result, HeldSeriesPass)
        assert Ticket.objects.filter(held_pass=result).count() == 2

    def test_past_covered_events_not_checked(
        self, organization: Organization, event_series: EventSeries, free_pass: SeriesPass, buyer: RevelUser
    ) -> None:
        _covered(organization, event_series, free_pass, "pa1")
        _covered(organization, event_series, free_pass, "pa3", days=9)
        past, _ = _covered(organization, event_series, free_pass, "pa2", days=-2, requires_full_profile=True)
        Event.objects.filter(pk=past.pk).update(status=Event.EventStatus.DRAFT, end=timezone.now() - timedelta(days=1))

        result = SeriesPassPurchaseService(free_pass, buyer).purchase()

        assert isinstance(result, HeldSeriesPass)
        assert not Ticket.objects.filter(held_pass=result, event=past).exists()


class TestPurchaseEventCapacity:
    @pytest.mark.parametrize("role", ["buyer", "owner", "staff"])
    def test_full_event_refused_with_429_nothing_written(
        self,
        organization: Organization,
        event_series: EventSeries,
        free_pass: SeriesPass,
        buyer: RevelUser,
        revel_user_factory: RevelUserFactory,
        role: str,
    ) -> None:
        """Event capacity applies to everyone: owners and staff are exempt from gates, not from capacity."""
        user = buyer if role == "buyer" else _privileged_user(role, organization, revel_user_factory)
        _, t1 = _covered(organization, event_series, free_pass, "cp1")
        full, t2 = _covered(organization, event_series, free_pass, "cp2", days=8, max_attendees=1)
        other = RevelUser.objects.create_user(username="other@example.com", email="other@example.com")
        Ticket.objects.create(event=full, tier=t2, user=other, status=Ticket.TicketStatus.ACTIVE, guest_name="Other")
        TicketTier.objects.filter(pk=t2.pk).update(quantity_sold=1)

        with pytest.raises(HttpError) as exc:
            SeriesPassPurchaseService(free_pass, user).purchase()

        assert exc.value.status_code == 429
        assert full.name in str(exc.value.message)
        assert not HeldSeriesPass.objects.filter(series_pass=free_pass).exists()
        assert Ticket.objects.filter(tier__in=[t1, t2]).count() == 1  # only the pre-existing one
        free_pass.refresh_from_db()
        assert free_pass.quantity_sold == 0
        t1.refresh_from_db()
        t2.refresh_from_db()
        assert (t1.quantity_sold, t2.quantity_sold) == (0, 1)

    def test_own_pending_waitlist_offer_counts_against_buyer(
        self, organization: Organization, event_series: EventSeries, free_pass: SeriesPass, buyer: RevelUser
    ) -> None:
        """The pass never claims the buyer's offer, so it must not be treated as reserved for them."""
        _, t1 = _covered(organization, event_series, free_pass, "wo1")
        event, t2 = _covered(organization, event_series, free_pass, "wo2", days=8, max_attendees=2)
        other = RevelUser.objects.create_user(username="other@example.com", email="other@example.com")
        Ticket.objects.create(event=event, tier=t2, user=other, status=Ticket.TicketStatus.ACTIVE, guest_name="Other")
        WaitlistOffer.objects.create(
            event=event, user=buyer, expires_at=timezone.now() + timedelta(hours=1), batch_id=uuid4()
        )

        with pytest.raises(HttpError) as exc:
            SeriesPassPurchaseService(free_pass, buyer).purchase()

        assert exc.value.status_code == 429
        assert not HeldSeriesPass.objects.filter(series_pass=free_pass).exists()

    def test_gates_refuse_before_taking_the_pass_lock(
        self, organization: Organization, event_series: EventSeries, free_pass: SeriesPass, buyer: RevelUser
    ) -> None:
        _covered(organization, event_series, free_pass, "lk1")
        _covered(organization, event_series, free_pass, "lk2", days=8)
        Blacklist.objects.create(
            organization=organization, first_name="John", last_name="Smith", created_by=organization.owner
        )

        with patch.object(SeriesPass.objects, "select_for_update") as lock:
            with pytest.raises(UserIsIneligibleError):
                SeriesPassPurchaseService(free_pass, buyer).purchase()

        lock.assert_not_called()


class TestBackfillAndExtensionSkips:
    @pytest.fixture
    def holder_pass(
        self, organization: Organization, event_series: EventSeries, free_pass: SeriesPass, buyer: RevelUser
    ) -> HeldSeriesPass:
        _covered(organization, event_series, free_pass, "base")
        _covered(organization, event_series, free_pass, "base2", days=9)
        result = SeriesPassPurchaseService(free_pass, buyer).purchase()
        assert isinstance(result, HeldSeriesPass)
        return result

    @staticmethod
    def _fill(event: Event, tier: TicketTier) -> None:
        filler = RevelUser.objects.create_user(username=f"f-{event.slug}@example.com", email=f"f-{event.slug}@x.com")
        Ticket.objects.create(event=event, tier=tier, user=filler, status=Ticket.TicketStatus.ACTIVE, guest_name="F")

    def test_backfill_skips_full_event_and_grants_the_rest(
        self,
        organization: Organization,
        event_series: EventSeries,
        free_pass: SeriesPass,
        holder_pass: HeldSeriesPass,
    ) -> None:
        full, full_tier = _covered(organization, event_series, free_pass, "bf-full", max_attendees=1)
        open_event, _ = _covered(organization, event_series, free_pass, "bf-open")
        self._fill(full, full_tier)

        created = series_pass_service.backfill_missing_tickets(holder_pass)

        assert [ticket.event_id for ticket in created] == [open_event.id]
        assert not Ticket.objects.filter(held_pass=holder_pass, event=full).exists()

    def test_backfill_skips_blacklisted_holder(
        self,
        organization: Organization,
        event_series: EventSeries,
        free_pass: SeriesPass,
        holder_pass: HeldSeriesPass,
        buyer: RevelUser,
    ) -> None:
        event, tier = _covered(organization, event_series, free_pass, "bf-bl")
        Blacklist.objects.create(
            organization=organization, first_name="John", last_name="Smith", created_by=organization.owner
        )

        assert series_pass_service.backfill_missing_tickets(holder_pass) == []

        assert not Ticket.objects.filter(held_pass=holder_pass, event=event).exists()
        tier.refresh_from_db()
        assert tier.quantity_sold == 0

    def test_extension_skips_full_event_and_blacklisted_holder(
        self,
        organization: Organization,
        event_series: EventSeries,
        free_pass: SeriesPass,
        holder_pass: HeldSeriesPass,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        clean_user = revel_user_factory(username="clean@example.com", email="clean@example.com", first_name="Ann")
        clean_pass = HeldSeriesPass.objects.create(
            series_pass=free_pass,
            user=clean_user,
            price_paid=Decimal("20.00"),
            status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
        )
        HeldSeriesPass.objects.filter(pk=holder_pass.pk).update(status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE)
        full, full_tier = _covered(organization, event_series, free_pass, "ex-full", max_attendees=1)
        open_event, _ = _covered(organization, event_series, free_pass, "ex-open")
        self._fill(full, full_tier)
        # holder_pass's user (John Smith) is now blacklisted by name; clean_user is not.
        Blacklist.objects.create(
            organization=organization, first_name="John", last_name="Smith", created_by=organization.owner
        )

        with patch("notifications.signals.series_pass.send_series_pass_extended"):
            materialize_series_pass_holders(str(free_pass.id), [str(full.id), str(open_event.id)])

        assert not Ticket.objects.filter(held_pass=holder_pass, event__in=[full, open_event]).exists()
        assert not Ticket.objects.filter(held_pass=clean_pass, event=full).exists()
        assert Ticket.objects.filter(held_pass=clean_pass, event=open_event).exists()

    @pytest.mark.parametrize("role", ["owner", "staff"])
    def test_backfill_and_extension_exempt_owner_and_staff(
        self,
        organization: Organization,
        event_series: EventSeries,
        free_pass: SeriesPass,
        revel_user_factory: RevelUserFactory,
        role: str,
    ) -> None:
        """A privileged holder whose name fuzzy-matches the blacklist still gets both kinds of tickets."""
        user = _privileged_user(role, organization, revel_user_factory)
        _covered(organization, event_series, free_pass, "pv1")
        _covered(organization, event_series, free_pass, "pv2", days=9)
        held = SeriesPassPurchaseService(free_pass, user).purchase()
        assert isinstance(held, HeldSeriesPass)
        HeldSeriesPass.objects.filter(pk=held.pk).update(status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE)
        Blacklist.objects.create(
            organization=organization, first_name="John", last_name="Smith", created_by=organization.owner
        )
        backfill_event, _ = _covered(organization, event_series, free_pass, "pv-bf")
        extension_event, _ = _covered(organization, event_series, free_pass, "pv-ex", days=10)

        created = series_pass_service.backfill_missing_tickets(held)
        assert {ticket.event_id for ticket in created} == {backfill_event.id, extension_event.id}

        Ticket.objects.filter(held_pass=held, event=extension_event).delete()
        with patch("notifications.signals.series_pass.send_series_pass_extended"):
            materialize_series_pass_holders(str(free_pass.id), [str(extension_event.id)])
        assert Ticket.objects.filter(held_pass=held, event=extension_event).exists()

    def test_extension_redispatch_does_not_report_already_ticketed_full_event_as_skipped(
        self,
        organization: Organization,
        event_series: EventSeries,
        free_pass: SeriesPass,
        holder_pass: HeldSeriesPass,
    ) -> None:
        HeldSeriesPass.objects.filter(pk=holder_pass.pk).update(status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE)
        event, tier = _covered(organization, event_series, free_pass, "rd", max_attendees=1)
        Ticket.objects.create(
            event=event,
            tier=tier,
            user=holder_pass.user,
            held_pass=holder_pass,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name="John Smith",
        )  # the holder already holds the only seat: the event is "full" because of them

        with patch("events.tasks.series_pass.logger") as log:
            materialize_series_pass_holders(str(free_pass.id), [str(event.id)])

        done = [c for c in log.info.call_args_list if c.args and c.args[0] == "series_pass_extension_done"]
        assert done[0].kwargs["skipped"] == 0
