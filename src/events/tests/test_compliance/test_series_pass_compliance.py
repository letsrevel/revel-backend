"""The series-pass quote exposes the pass checkout's payment-channel decision (#1081)."""

import typing as t
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.compliance import PaymentChannelCapability
from events.management.commands.bootstrap_helpers.compliance import create_compliance_fixtures
from events.models import EventSeries, Organization, SeriesPass, TicketTier
from events.tests.test_series_pass.test_backfill import _make_covered_event, _make_pass

pytestmark = pytest.mark.django_db


def _quote(series_pass: SeriesPass) -> dict[str, t.Any]:
    response = Client().get(reverse("api:get_series_pass_quote", kwargs={"pass_id": series_pass.pk}))
    assert response.status_code == 200, response.content
    return t.cast(dict[str, t.Any], response.json())


def _checkout_status(series_pass: SeriesPass, user: RevelUser) -> int:
    client = Client(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")  # type: ignore[attr-defined]
    response = client.post(
        reverse("api:checkout_series_pass", kwargs={"pass_id": series_pass.pk}), content_type="application/json"
    )
    return response.status_code


def test_seeded_it_season_pass_reads_blocked_and_checkout_refuses(nonmember_user: RevelUser) -> None:
    create_compliance_fixtures(timezone.now())
    series_pass = SeriesPass.objects.get(name="IT Season Pass")

    assert _quote(series_pass)["compliance"] == {"online_payment": PaymentChannelCapability.BLOCKED}
    assert _checkout_status(series_pass, nonmember_user) == 422


def test_seeded_it_season_pass_switched_offline_reads_allowed(nonmember_user: RevelUser) -> None:
    create_compliance_fixtures(timezone.now())
    series_pass = SeriesPass.objects.get(name="IT Season Pass")
    SeriesPass.objects.filter(pk=series_pass.pk).update(payment_method=TicketTier.PaymentMethod.OFFLINE)

    assert _quote(series_pass)["compliance"] == {"online_payment": PaymentChannelCapability.ALLOWED}
    assert _checkout_status(series_pass, nonmember_user) == 200


@pytest.mark.parametrize(
    ("country", "payment_method", "price", "expected"),
    [
        ("IT", TicketTier.PaymentMethod.ONLINE, "20.00", PaymentChannelCapability.BLOCKED),
        ("IT", TicketTier.PaymentMethod.ONLINE, "0.00", PaymentChannelCapability.ALLOWED),
        ("IT", TicketTier.PaymentMethod.OFFLINE, "20.00", PaymentChannelCapability.ALLOWED),
        ("IT", TicketTier.PaymentMethod.FREE, "0.00", PaymentChannelCapability.ALLOWED),
        ("AT", TicketTier.PaymentMethod.ONLINE, "20.00", PaymentChannelCapability.ALLOWED),
    ],
)
def test_quote_compliance_matches_the_checkout_gate(
    organization: Organization,
    event_series: EventSeries,
    nonmember_user: RevelUser,
    country: str,
    payment_method: str,
    price: str,
    expected: PaymentChannelCapability,
) -> None:
    """``blocked`` exactly when ``POST /series-passes/{id}/checkout`` answers 422."""
    Organization.objects.filter(pk=organization.pk).update(
        vat_country_code=country,
        visibility=Organization.Visibility.PUBLIC,
        stripe_account_id="acct_series_pass_compliance",
        stripe_charges_enabled=True,
        stripe_details_submitted=True,
    )
    series_pass = _make_pass(organization, event_series, payment_method, f"pass-{country}-{payment_method}")
    SeriesPass.objects.filter(pk=series_pass.pk).update(price=Decimal(price))

    assert _quote(series_pass)["compliance"]["online_payment"] == expected
    assert (_checkout_status(series_pass, nonmember_user) == 422) is (expected == PaymentChannelCapability.BLOCKED)


def test_quote_query_count_does_not_grow_with_covered_events(
    organization: Organization, event_series: EventSeries
) -> None:
    """The decision reads each covered event's venue country from the one tier-links query."""
    Organization.objects.filter(pk=organization.pk).update(
        vat_country_code="IT", visibility=Organization.Visibility.PUBLIC
    )
    series_pass = _make_pass(organization, event_series, TicketTier.PaymentMethod.ONLINE, "count")
    url = reverse("api:get_series_pass_quote", kwargs={"pass_id": series_pass.pk})
    with CaptureQueriesContext(connection) as two_events:
        assert Client().get(url).status_code == 200

    for i in range(3):
        _make_covered_event(organization, event_series, series_pass, f"count-extra-{i}", timedelta(days=i + 5))
    with CaptureQueriesContext(connection) as five_events:
        assert Client().get(url).status_code == 200

    assert len(five_events) == len(two_events)
