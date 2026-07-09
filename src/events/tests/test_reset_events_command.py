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
from django.test import override_settings
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    EventSeries,
    HeldSeriesPass,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    SeriesPass,
    Ticket,
    TicketTier,
)

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
