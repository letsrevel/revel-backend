"""Tests for the platform-revenue section of the admin dashboard (#1390)."""

import typing as t
from decimal import Decimal

import pytest
from django.test import Client, RequestFactory
from django.urls import reverse
from freezegun import freeze_time

from accounts.models import RevelUser
from events.models import Payment, Ticket, TicketTier
from revel.dashboard import dashboard_callback

pytestmark = pytest.mark.django_db

FEE: dict[str, t.Any] = {
    "platform_fee": Decimal("1.20"),
    "platform_fee_net": Decimal("1.00"),
    "platform_fee_vat": Decimal("0.20"),
}


def test_windows_use_vienna_today_monday_and_first_of_month(
    superuser: RevelUser,
    tier_factory: t.Callable[..., TicketTier],
    ticket_factory: t.Callable[..., Ticket],
    payment_factory: t.Callable[..., Payment],
    django_user_model: type[RevelUser],
) -> None:
    tier = tier_factory(name="T")
    # "Now" is Wed 11 Mar 2026 10:00 Vienna. Boundaries (UTC): today 10 Mar 23:00, Monday 8 Mar 23:00, 1st 28 Feb 23:00.
    for i, stamp in enumerate(
        [
            "2026-02-28 22:59:59",  # last month
            "2026-02-28 23:00:00",  # this month only
            "2026-03-08 22:59:59",  # Sunday 23:59 Vienna: this month only
            "2026-03-08 23:00:00",  # Monday 00:00 Vienna: this week
            "2026-03-10 22:59:59",  # Tuesday 23:59 Vienna: this week only
            "2026-03-10 23:00:00",  # Wednesday 00:00 Vienna: today
        ]
    ):
        user = django_user_model.objects.create_user(username=f"w{i}", email=f"w{i}@x.com", password="x")
        with freeze_time(stamp):
            payment_factory(ticket_factory(user=user, tier=tier), **FEE)

    request = RequestFactory().get("/admin/")
    request.user = superuser
    with freeze_time("2026-03-11 09:00:00"):
        revenue = dashboard_callback(request, {})["dashboard"]["platform_revenue"]

    assert [len(revenue[k]) and revenue[k][0].gross for k in ("today", "week", "month")] == [
        Decimal("1.20"),
        Decimal("3.60"),
        Decimal("6.00"),
    ]
    assert revenue["mrr"] == []


def test_admin_index_renders_revenue_cards(
    settings: t.Any,
    superuser: RevelUser,
    tier_factory: t.Callable[..., TicketTier],
    ticket_factory: t.Callable[..., Ticket],
    payment_factory: t.Callable[..., Payment],
) -> None:
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    payment_factory(
        ticket_factory(user=superuser, tier=tier_factory(name="T")),
        platform_fee=Decimal("2.00"),
        platform_fee_net=None,
        platform_fee_vat=None,
    )
    client = Client()
    client.force_login(superuser)

    html = client.get(reverse("admin:index")).content.decode()

    assert "Platform revenue" in html
    assert "MRR (membership fees, net)" in html
    assert "EUR 2.00" in html
    assert "1 payment without VAT breakdown" in html
