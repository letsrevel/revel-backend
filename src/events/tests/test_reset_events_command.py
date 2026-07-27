"""Regression tests for the ``reset_events`` management command.

These tests focus on edge cases that previously caused
``django.db.models.deletion.ProtectedError`` during the demo-data reset path
exercised by the ``reset_demo_data`` Celery task.
"""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db.models import CASCADE, PROTECT, RESTRICT, Model
from django.test import override_settings
from django.utils import timezone

from accounts.models import Referral, ReferralCode, ReferralPayout, ReferralPayoutStatement, RevelUser
from events.models import (
    Event,
    EventSeries,
    HeldSeriesPass,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMembershipRequest,
    RecurrenceRule,
    SeriesPass,
    Ticket,
    TicketTier,
)
from questionnaires.models import Questionnaire

pytestmark = pytest.mark.django_db


@pytest.fixture
def demo_user(django_user_model: t.Type[RevelUser]) -> RevelUser:
    """Create a user that will be wiped by ``reset_events`` (non-@letsrevel.io)."""
    return django_user_model.objects.create_user(
        username="demo_owner",
        email="demo_owner@example.com",
        password="pass",
    )


@pytest.fixture
def demo_subscriber(django_user_model: t.Type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="demo_subscriber",
        email="demo_subscriber@example.com",
        password="pass",
    )


@pytest.fixture
def demo_organization(demo_user: RevelUser) -> Organization:
    return Organization.objects.create(
        name="Demo Org",
        slug="demo-org",
        owner=demo_user,
    )


@pytest.fixture
def demo_tier(demo_organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=demo_organization, name="Pro")


@pytest.fixture
def demo_plan(demo_tier: MembershipTier) -> MembershipSubscriptionPlan:
    return MembershipSubscriptionPlan.objects.create(
        tier=demo_tier,
        name="Monthly",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
        period_count=1,
    )


class TestResetEventsCommand:
    """Regression coverage for ``python manage.py reset_events --no-input``."""

    @override_settings(DEMO_MODE=True)
    def test_succeeds_with_active_membership_subscription(
        self,
        demo_organization: Organization,
        demo_subscriber: RevelUser,
        demo_plan: MembershipSubscriptionPlan,
    ) -> None:
        """Regression for issue #434.

        An active ``MembershipSubscription`` previously aborted the Organization
        cascade via the ``MembershipSubscriptionPlan ← MembershipSubscription``
        PROTECT FK, which raised ``ProtectedError`` and bubbled up to the
        ``reset_demo_data`` Celery task. The demo-reset path must delete
        subscriptions explicitly before deleting organizations.
        """
        subscription = MembershipSubscription.objects.create(
            user=demo_subscriber,
            plan=demo_plan,
            organization=demo_organization,
            status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        )

        with patch("events.management.commands.reset_events.call_command") as mocked_call:
            call_command("reset_events", "--no-input")
            # bootstrap_events should be invoked exactly once at the end.
            mocked_call.assert_called_once_with("bootstrap_events")

        assert not Organization.objects.filter(pk=demo_organization.pk).exists()
        assert not MembershipSubscription.objects.filter(pk=subscription.pk).exists()
        # The subscriber user used a non-@letsrevel.io address, so they should
        # also have been swept up by the demo-user cleanup.
        assert not RevelUser.objects.filter(pk=demo_subscriber.pk).exists()

    @override_settings(DEMO_MODE=True)
    def test_succeeds_with_tier_bearing_membership_application(
        self,
        demo_organization: Organization,
        demo_subscriber: RevelUser,
        demo_tier: MembershipTier,
        demo_plan: MembershipSubscriptionPlan,
    ) -> None:
        """Regression for issue #794.

        ``OrganizationMembershipRequest.tier`` and ``.plan`` are PROTECT FKs, so
        any application row (in any status) aborted the Organization cascade when
        it reached ``MembershipTier``. The demo-reset path must delete
        application rows explicitly before deleting organizations.
        """
        application = OrganizationMembershipRequest.objects.create(
            organization=demo_organization,
            user=demo_subscriber,
            tier=demo_tier,
            plan=demo_plan,
            status=OrganizationMembershipRequest.Status.COMPLETED,
        )

        with patch("events.management.commands.reset_events.call_command") as mocked_call:
            call_command("reset_events", "--no-input")
            mocked_call.assert_called_once_with("bootstrap_events")

        assert not Organization.objects.filter(pk=demo_organization.pk).exists()
        assert not OrganizationMembershipRequest.objects.filter(pk=application.pk).exists()
        assert not MembershipTier.objects.filter(pk=demo_tier.pk).exists()

    @override_settings(DEMO_MODE=True)
    def test_succeeds_with_held_series_pass(
        self,
        demo_organization: Organization,
        demo_subscriber: RevelUser,
    ) -> None:
        """Regression for the ``HeldSeriesPass.series_pass`` PROTECT / ``Ticket.held_pass`` RESTRICT chain.

        A purchased series pass previously aborted the Organization cascade: the
        cascade reaches ``EventSeries → SeriesPass``, but ``HeldSeriesPass``
        PROTECTs its ``series_pass``. Materialized pass tickets additionally
        RESTRICT their ``held_pass``. The demo-reset path must delete those
        tickets and the held passes before deleting organizations.
        """
        series = EventSeries.objects.create(organization=demo_organization, name="Weekly", slug="weekly")
        series_pass = SeriesPass.objects.create(
            event_series=series,
            name="Season Ticket",
            price=Decimal("36.00"),
            pro_rata_discount=Decimal("6.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.ONLINE,
        )
        held_pass = HeldSeriesPass.objects.create(
            series_pass=series_pass,
            user=demo_subscriber,
            status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
            price_paid=Decimal("36.00"),
        )
        event = Event.objects.create(
            organization=demo_organization,
            name="Class 1",
            slug="class-1",
            event_series=series,
            start=timezone.now(),
        )
        tier = TicketTier.objects.create(event=event, name="Tier", price=Decimal("10.00"), currency="EUR")
        ticket = Ticket.objects.create(
            event=event,
            user=demo_subscriber,
            tier=tier,
            guest_name="Demo Subscriber",
            held_pass=held_pass,
        )

        with patch("events.management.commands.reset_events.call_command") as mocked_call:
            call_command("reset_events", "--no-input")
            mocked_call.assert_called_once_with("bootstrap_events")

        assert not Organization.objects.filter(pk=demo_organization.pk).exists()
        assert not HeldSeriesPass.objects.filter(pk=held_pass.pk).exists()
        assert not Ticket.objects.filter(pk=ticket.pk).exists()
        assert not SeriesPass.objects.filter(pk=series_pass.pk).exists()


# Every model reset_events deletes, explicitly or as a final cascade root.
# Keep in sync with the command — the guard test below walks the deletion
# graph from these roots.
RESET_DELETE_ROOTS: tuple[type[Model], ...] = (
    ReferralPayoutStatement,
    ReferralPayout,
    Referral,
    ReferralCode,
    RecurrenceRule,
    OrganizationMembershipRequest,
    MembershipSubscription,
    Ticket,
    HeldSeriesPass,
    Organization,
    Questionnaire,
    RevelUser,
)

# PROTECT/RESTRICT FK edges into the reset cascade that reset_events already
# handles, as (source model label, FK field name, target model label). Each
# entry is either pre-deleted or nulled by the command before the edge's
# target is deleted.
HANDLED_PROTECTED_EDGES: frozenset[tuple[str, str, str]] = frozenset(
    {
        # Referral chain: pre-deleted in dependency order (statement → payout → referral → code).
        ("accounts.ReferralPayoutStatement", "payout", "accounts.ReferralPayout"),
        ("accounts.ReferralPayout", "referral", "accounts.Referral"),
        ("accounts.Referral", "referral_code", "accounts.ReferralCode"),
        ("accounts.ReferralCode", "user", "accounts.RevelUser"),
        ("accounts.Referral", "referrer", "accounts.RevelUser"),
        ("accounts.Referral", "referred_user", "accounts.RevelUser"),
        # Organizations are deleted before the user sweep.
        ("events.Organization", "owner", "accounts.RevelUser"),
        # Series templates: FKs nulled via EventSeries.objects.update(...) first.
        ("events.EventSeries", "template_event", "events.Event"),
        ("events.EventSeries", "recurrence_rule", "events.RecurrenceRule"),
        # Series passes: pass-backed tickets then held passes are pre-deleted.
        ("events.Ticket", "held_pass", "events.HeldSeriesPass"),
        ("events.HeldSeriesPass", "series_pass", "events.SeriesPass"),
        # Membership applications and subscriptions are pre-deleted (issues #434, #794).
        ("events.OrganizationMembershipRequest", "tier", "events.MembershipTier"),
        ("events.OrganizationMembershipRequest", "plan", "events.MembershipSubscriptionPlan"),
        ("events.MembershipSubscription", "plan", "events.MembershipSubscriptionPlan"),
        ("events.MembershipSubscription", "pending_plan", "events.MembershipSubscriptionPlan"),
    }
)


def test_no_unhandled_protected_edges_into_reset_cascade() -> None:
    """Guard: a new PROTECT/RESTRICT FK into the reset cascade must be handled in ``reset_events``.

    Every pre-cascade delete in the command was added reactively after a
    ``ProtectedError`` in the demo reset (#434, series passes, #794). This test
    walks the deletion graph instead: it computes the CASCADE closure of every
    model the command deletes, then flags any PROTECT or RESTRICT FK pointing
    at a model in that closure. PROTECT aborts a delete even when the
    protecting rows are part of the same cascade, and RESTRICT only tolerates
    same-operation deletes — the command runs several — so every such edge
    needs explicit handling (a pre-delete or FK nulling) and a matching entry
    in ``HANDLED_PROTECTED_EDGES``.
    """
    closure: set[type[Model]] = set(RESET_DELETE_ROOTS)
    queue: list[type[Model]] = list(RESET_DELETE_ROOTS)
    while queue:
        model = queue.pop()
        for rel in model._meta.related_objects:
            on_delete = getattr(rel, "on_delete", None) or getattr(rel.field, "on_delete", None)
            related_model = t.cast(type[Model], rel.related_model)
            if on_delete is CASCADE and related_model not in closure:
                closure.add(related_model)
                queue.append(related_model)

    hazard_edges: set[tuple[str, str, str]] = set()
    for model in closure:
        for rel in model._meta.related_objects:
            on_delete = getattr(rel, "on_delete", None) or getattr(rel.field, "on_delete", None)
            if on_delete is PROTECT or on_delete is RESTRICT:
                source = t.cast(type[Model], rel.related_model)
                hazard_edges.add((source._meta.label, rel.field.name, model._meta.label))

    unhandled = hazard_edges - HANDLED_PROTECTED_EDGES
    assert not unhandled, (
        "New PROTECT/RESTRICT FK(s) point into the reset_events deletion cascade; "
        "reset_events will crash with ProtectedError once such rows exist. Handle "
        "each edge in the command (pre-delete or null the FK before the cascade) "
        f"and add it to HANDLED_PROTECTED_EDGES: {sorted(unhandled)}"
    )

    stale = HANDLED_PROTECTED_EDGES - hazard_edges
    assert not stale, f"HANDLED_PROTECTED_EDGES lists edges that no longer exist — remove them: {sorted(stale)}"
