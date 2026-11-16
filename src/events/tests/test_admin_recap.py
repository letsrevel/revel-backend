"""Tests for the daily admin activity recap (#1390)."""

import typing as t
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.test import override_settings
from freezegun import freeze_time

from accounts.models import RevelUser
from events.models import (
    Event,
    EventRSVP,
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    Payment,
    Ticket,
    TicketTier,
)
from events.service import admin_recap_service
from events.service.platform_revenue_service import FeeTotals
from events.tasks import send_admin_recap_discord, send_admin_recap_pushover, send_daily_admin_recap

pytestmark = pytest.mark.django_db

# Vienna is UTC+1 on 2026-03-10, so the previous local day (Mar 9) is [Mar 8 23:00, Mar 9 23:00) UTC.
NOW = datetime(2026, 3, 10, 8, 0, tzinfo=UTC)
JUST_BEFORE = "2026-03-08 22:59:59"
START = "2026-03-08 23:00:00"
LAST_INSIDE = "2026-03-09 22:59:59"
END = "2026-03-09 23:00:00"


def test_window_is_previous_vienna_day() -> None:
    start, end = admin_recap_service.previous_local_day_window(NOW)
    assert start == datetime(2026, 3, 8, 23, 0, tzinfo=UTC)
    assert end == datetime(2026, 3, 9, 23, 0, tzinfo=UTC)


def test_window_across_dst_change() -> None:
    """Previous day is Sun 29 Mar 2026 (clocks go forward): 23 hours long."""
    start, end = admin_recap_service.previous_local_day_window(datetime(2026, 3, 30, 8, 0, tzinfo=UTC))
    assert start == datetime(2026, 3, 28, 23, 0, tzinfo=UTC)
    assert end == datetime(2026, 3, 29, 22, 0, tzinfo=UTC)


@pytest.fixture
def tier(tier_factory: t.Callable[..., TicketTier]) -> TicketTier:
    return tier_factory(name="T")


def _populate(
    organization: Organization,
    event: Event,
    ticket_factory: t.Callable[..., Ticket],
    payment_factory: t.Callable[..., Payment],
    django_user_model: type[RevelUser],
    tier: TicketTier,
    stamp: str,
    suffix: str,
    **extra: t.Any,
) -> None:
    """Create one of everything at ``stamp``."""
    with freeze_time(stamp):
        django_user_model.objects.create_user(username=f"reg{suffix}", email=f"reg{suffix}@x.com", password="x")
        guest = django_user_model.objects.create_user(
            username=f"g{suffix}", email=f"g{suffix}@x.com", password="x", guest=True
        )
        Organization.objects.create(name=f"Org {suffix}", slug=f"org-{suffix}", owner=organization.owner)
        Event.objects.create(
            organization=organization, name=f"Ev {suffix}", slug=f"ev-{suffix}", start=NOW, status="open"
        )
        ticket = ticket_factory(user=guest, tier=tier)
        EventRSVP.objects.create(event=event, user=guest, status=EventRSVP.RsvpStatus.YES)
        payment_factory(ticket, **extra)


def test_counts_only_inside_window(
    organization: Organization,
    event: Event,
    ticket_factory: t.Callable[..., Ticket],
    payment_factory: t.Callable[..., Payment],
    django_user_model: type[RevelUser],
    tier: TicketTier,
) -> None:
    args = (organization, event, ticket_factory, payment_factory, django_user_model, tier)
    _populate(*args, JUST_BEFORE, "before", amount=Decimal("1.00"))
    _populate(*args, START, "start", amount=Decimal("10.00"), currency="EUR")
    _populate(*args, LAST_INSIDE, "last", amount=Decimal("5.50"), currency="EUR")
    _populate(*args, END, "end", amount=Decimal("2.00"))

    recap = admin_recap_service.build_daily_recap(NOW)

    assert (recap.registered_users, recap.guest_users) == (2, 2)
    assert recap.organization_names == ["Org start", "Org last"]
    assert (recap.events, recap.tickets, recap.rsvps) == (2, 2, 2)
    assert recap.payments.count == 2
    assert recap.payments.by_currency == {"EUR": Decimal("15.50")}


def test_payments_collected_grouped_per_currency(
    event: Event,
    ticket_factory: t.Callable[..., Ticket],
    payment_factory: t.Callable[..., Payment],
    django_user_model: type[RevelUser],
    organization: Organization,
    tier: TicketTier,
) -> None:
    with freeze_time(START):
        users = [
            django_user_model.objects.create_user(username=f"p{i}", email=f"p{i}@x.com", password="x") for i in range(5)
        ]
        payment_factory(ticket_factory(tier=tier, user=users[0]), amount=Decimal("10.00"), currency="EUR")
        payment_factory(ticket_factory(tier=tier, user=users[1]), amount=Decimal("5.00"), currency="EUR")
        payment_factory(ticket_factory(tier=tier, user=users[2]), amount=Decimal("20.00"), currency="CHF")
        payment_factory(
            ticket_factory(tier=tier, user=users[3]), amount=Decimal("99.00"), status=Payment.PaymentStatus.PENDING
        )
        payment_factory(  # collected, then refunded: still counted (the fee is kept)
            ticket_factory(tier=tier, user=users[4]), amount=Decimal("7.00"), status=Payment.PaymentStatus.REFUNDED
        )

    recap = admin_recap_service.build_daily_recap(NOW)

    assert recap.payments.count == 4
    assert recap.payments.by_currency == {"CHF": Decimal("20.00"), "EUR": Decimal("22.00")}


def test_membership_payments_use_occurred_at_and_succeeded_only(
    organization: Organization, django_user_model: type[RevelUser]
) -> None:
    user = django_user_model.objects.create_user(username="m", email="m@x.com", password="x")
    tier = MembershipTier.objects.create(organization=organization, name="Pro")
    plan = MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
        period_count=1,
    )
    sub = MembershipSubscription.objects.create(user=user, plan=plan, organization=organization)
    base: dict[str, t.Any] = {"subscription": sub, "currency": "EUR", "period_start": NOW, "period_end": NOW}
    with freeze_time(END):  # created just outside the window, but occurred inside it
        MembershipPayment.objects.create(
            amount=Decimal("7.00"), occurred_at=datetime(2026, 3, 9, 12, tzinfo=UTC), **base
        )
        MembershipPayment.objects.create(amount=Decimal("3.00"), status=MembershipPayment.PaymentStatus.FAILED, **base)
    with freeze_time(START):
        MembershipPayment.objects.create(amount=Decimal("4.00"), **base)

    recap = admin_recap_service.build_daily_recap(NOW)

    assert recap.membership_payments.count == 2
    assert recap.membership_payments.by_currency == {"EUR": Decimal("11.00")}


def test_format_caps_org_names() -> None:
    recap = admin_recap_service.DailyRecap(
        start=datetime(2026, 11, 15, tzinfo=UTC),
        end=datetime(2026, 11, 16, tzinfo=UTC),
        registered_users=3,
        guest_users=41,
        organization_names=[f"O{i}" for i in range(12)],
        events=5,
        tickets=120,
        rsvps=33,
        payments=admin_recap_service.PaymentTotals(18, {"CHF": Decimal("20"), "EUR": Decimal("540")}),
        membership_payments=admin_recap_service.PaymentTotals(),
        fees={"EUR": FeeTotals("EUR", gross=Decimal("50.52"), net=Decimal("42.10"), vat=Decimal("8.42"))},
    )

    text = admin_recap_service.format_daily_recap(recap)

    assert text.splitlines() == [
        "Revel daily recap — Sun 15 Nov 2026",
        "Users: 3 registered, 41 guests",
        "Orgs: 12 (O0, O1, O2, O3, O4, O5, O6, O7, O8, O9 and 2 more)",
        "Events: 5 · Tickets: 120 · RSVPs: 33",
        "Payments collected: 18 (CHF 20.00, EUR 540.00)",
        "Membership payments collected: 0 (none)",
        "Fees: EUR 42.10 net + 8.42 VAT (set aside) = 50.52 gross",
    ]


def test_format_fits_pushover_limit_with_long_names_and_many_currencies() -> None:
    currencies = ["EUR", "CHF", "GBP", "USD", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF"]
    fee = {
        c: FeeTotals(c, gross=Decimal("123456.78"), net=Decimal("100000.00"), vat=Decimal("23456.78"))
        for c in currencies
    }
    totals = admin_recap_service.PaymentTotals(500, {c: Decimal("1234567.89") for c in currencies})
    recap = admin_recap_service.DailyRecap(
        start=datetime(2026, 11, 15, tzinfo=UTC),
        end=datetime(2026, 11, 16, tzinfo=UTC),
        registered_users=3,
        guest_users=41,
        organization_names=[f"{i}" + "x" * 500 for i in range(30)],
        events=5,
        tickets=120,
        rsvps=33,
        payments=totals,
        membership_payments=totals,
        fees=fee,
    )

    text = admin_recap_service.format_daily_recap(recap)

    assert len(text) <= admin_recap_service.MAX_MESSAGE_LEN
    assert "Orgs: 30" in text
    assert "x" * 41 not in text  # names are clipped to 40 chars


def _ok() -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.raise_for_status.return_value = None
    return response


def _status_error(status: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError("boom", request=MagicMock(), response=MagicMock(status_code=status))


@override_settings(PUSHOVER_USER_KEY="k", PUSHOVER_APP_TOKEN="t", DISCORD_ADMIN_WEBHOOK_URL="https://example.com/hook")
def test_beat_task_queues_each_configured_channel() -> None:
    with (
        patch("events.tasks.admin_recap.send_admin_recap_pushover.delay") as pushover,
        patch("events.tasks.admin_recap.send_admin_recap_discord.delay") as discord,
    ):
        result = send_daily_admin_recap()

    assert result == {"pushover": "queued", "discord": "queued"}
    assert "Revel daily recap" in pushover.call_args.args[0]
    assert discord.call_args.args[0] == pushover.call_args.args[0]


@override_settings(PUSHOVER_USER_KEY="", PUSHOVER_APP_TOKEN="", DISCORD_ADMIN_WEBHOOK_URL=None)
def test_beat_task_skips_unconfigured_channels() -> None:
    with (
        patch("events.tasks.admin_recap.send_admin_recap_pushover.delay") as pushover,
        patch("events.tasks.admin_recap.send_admin_recap_discord.delay") as discord,
    ):
        result = send_daily_admin_recap()

    assert result == {"pushover": "skipped", "discord": "skipped"}
    pushover.assert_not_called()
    discord.assert_not_called()


@override_settings(PUSHOVER_USER_KEY="k", PUSHOVER_APP_TOKEN="t", DISCORD_ADMIN_WEBHOOK_URL="https://example.com/hook")
def test_channel_tasks_post_expected_payloads() -> None:
    with patch("events.tasks.admin_recap.httpx.post", return_value=_ok()) as post:
        assert send_admin_recap_pushover("hello")["status"] == "sent"
        assert send_admin_recap_discord("hello")["status"] == "sent"

    assert post.call_args_list[0].kwargs["data"]["message"] == "hello"
    assert post.call_args_list[1].kwargs["json"] == {"content": "hello", "allowed_mentions": {"parse": []}}


@override_settings(DISCORD_ADMIN_WEBHOOK_URL="https://example.com/hook")
def test_channel_task_does_not_retry_on_4xx() -> None:
    response = _ok()
    response.raise_for_status.side_effect = _status_error(400)
    with (
        patch("events.tasks.admin_recap.httpx.post", return_value=response),
        patch.object(send_admin_recap_discord, "retry") as retry,
    ):
        result = send_admin_recap_discord("hello")

    assert result == {"status": "failed", "channel": "discord"}
    retry.assert_not_called()


@pytest.mark.parametrize("failure", [_status_error(503), httpx.ConnectTimeout("slow")])
@override_settings(DISCORD_ADMIN_WEBHOOK_URL="https://example.com/hook")
def test_channel_task_retries_on_5xx_and_network_errors(failure: Exception) -> None:
    with (
        patch("events.tasks.admin_recap.httpx.post", side_effect=failure),
        patch.object(send_admin_recap_discord, "retry", side_effect=RuntimeError("retried")) as retry,
        pytest.raises(RuntimeError, match="retried"),
    ):
        send_admin_recap_discord("hello")

    retry.assert_called_once()
