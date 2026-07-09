"""Tests for the ``series_pass`` ticket annotation and the admin attendee ``source`` filter.

Covers: my-tickets response shape, query-count stability (no N+1 as pass-ticket rows
grow), and the admin ticket list's ``source=pass``/``source=direct`` filter.
"""

from decimal import Decimal

import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Event, EventSeries, HeldSeriesPass, SeriesPass, Ticket, TicketTier

pytestmark = pytest.mark.django_db


@pytest.fixture
def revel_user_client(revel_user: RevelUser) -> Client:
    """API client for the series-pass holder."""
    refresh = RefreshToken.for_user(revel_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def owner_client(organization_owner_user: RevelUser) -> Client:
    """API client for the organization owner (admin ticket list)."""
    refresh = RefreshToken.for_user(organization_owner_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def held_pass(series_pass: SeriesPass, revel_user: RevelUser) -> HeldSeriesPass:
    return HeldSeriesPass.objects.create(
        series_pass=series_pass,
        user=revel_user,
        status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
        price_paid=series_pass.price,
    )


@pytest.fixture
def pass_ticket(event: Event, ticket_tier: TicketTier, revel_user: RevelUser, held_pass: HeldSeriesPass) -> Ticket:
    """The materialized per-event ticket for the holder's series pass."""
    return Ticket.objects.create(
        guest_name="Pass Holder",
        user=revel_user,
        event=event,
        tier=ticket_tier,
        status=Ticket.TicketStatus.ACTIVE,
        held_pass=held_pass,
    )


@pytest.fixture
def plain_ticket(event: Event, ticket_tier: TicketTier, revel_user: RevelUser) -> Ticket:
    """A regular (non-pass) ticket for the same user/event, no held_pass set."""
    return Ticket.objects.create(
        guest_name="Direct Buyer",
        user=revel_user,
        event=event,
        tier=ticket_tier,
        status=Ticket.TicketStatus.ACTIVE,
    )


def _create_extra_pass_ticket(event_series: EventSeries, held_pass: HeldSeriesPass, suffix: str) -> Ticket:
    """Create a new event + tier in the series and a pass ticket for it."""
    evt = Event.objects.create(
        organization=event_series.organization,
        name=f"Extra Event {suffix}",
        slug=f"extra-event-{suffix}",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        event_series=event_series,
        max_attendees=100,
        start=timezone.now(),
        status=Event.EventStatus.OPEN,
        requires_ticket=True,
    )
    tier = TicketTier.objects.create(
        event=evt,
        name=f"Tier {suffix}",
        price=Decimal("10.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )
    return Ticket.objects.create(
        guest_name="Pass Holder",
        user=held_pass.user,
        event=evt,
        tier=tier,
        status=Ticket.TicketStatus.ACTIVE,
        held_pass=held_pass,
    )


# ---- My-tickets response shape ----


def test_my_tickets_pass_ticket_carries_series_pass(
    revel_user_client: Client,
    pass_ticket: Ticket,
    plain_ticket: Ticket,
    held_pass: HeldSeriesPass,
    series_pass: SeriesPass,
) -> None:
    """A pass-derived ticket exposes {held_pass_id, series_pass_id, name}; a direct ticket exposes null."""
    url = reverse("api:dashboard_tickets")
    response = revel_user_client.get(url)
    assert response.status_code == 200
    by_id = {item["id"]: item for item in response.json()["results"]}

    assert by_id[str(pass_ticket.id)]["series_pass"] == {
        "held_pass_id": str(held_pass.id),
        "series_pass_id": str(series_pass.id),
        "name": series_pass.name,
    }
    assert by_id[str(plain_ticket.id)]["series_pass"] is None


# ---- Query-count stability ----


def test_my_tickets_query_count_does_not_grow_with_pass_tickets(
    revel_user_client: Client,
    pass_ticket: Ticket,
    plain_ticket: Ticket,
    held_pass: HeldSeriesPass,
    event_series: EventSeries,
) -> None:
    """Serializing a page with 1 pass + 1 direct ticket costs about the same as with 3 pass + 1 direct.

    We compare two list sizes rather than pin an absolute count: Silk profiling is active
    in this dev/test environment (``SILK_PROFILER=True``) and adds a couple of its own
    bookkeeping queries per request, so the raw count isn't stable across runs (see the
    identical rationale in ``polls/tests/test_controllers_list.py::test_list_polls_query_count_constant``).
    What must hold regardless is that each *additional* pass ticket doesn't add its own
    query (no N+1 on ``held_pass``/``held_pass__series_pass``).
    """
    url = reverse("api:dashboard_tickets")

    with CaptureQueriesContext(connection) as baseline_ctx:
        baseline_response = revel_user_client.get(url)
    assert baseline_response.status_code == 200
    assert baseline_response.json()["count"] == 2
    baseline_count = len(baseline_ctx.captured_queries)

    # Add 2 more pass tickets from a second series pass, across two new events in the series.
    second_pass = SeriesPass.objects.create(
        event_series=event_series,
        name="Second Season Ticket",
        price=Decimal("36.00"),
        pro_rata_discount=Decimal("6.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
    )
    second_held_pass = HeldSeriesPass.objects.create(
        series_pass=second_pass,
        user=held_pass.user,
        status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
        price_paid=second_pass.price,
    )
    _create_extra_pass_ticket(event_series, second_held_pass, "a")
    _create_extra_pass_ticket(event_series, second_held_pass, "b")

    with CaptureQueriesContext(connection) as scaled_ctx:
        scaled_response = revel_user_client.get(url)
    assert scaled_response.status_code == 200
    assert scaled_response.json()["count"] == 4
    scaled_count = len(scaled_ctx.captured_queries)

    # Two extra pass tickets were added; per-row growth must not scale (an N+1 on
    # held_pass/held_pass__series_pass would add at least 1 real query per row).
    additional_per_ticket = (scaled_count - baseline_count) / 2
    assert additional_per_ticket < 2, (
        f"Query count scaled with pass-ticket count: {baseline_count} queries for 2 tickets, "
        f"{scaled_count} for 4 tickets ({additional_per_ticket:.1f} per extra ticket)."
    )


# ---- Admin attendee-list source filter ----


def test_admin_list_tickets_filter_by_source(
    owner_client: Client,
    event: Event,
    pass_ticket: Ticket,
    plain_ticket: Ticket,
    held_pass: HeldSeriesPass,
    series_pass: SeriesPass,
) -> None:
    """?source=pass / ?source=direct isolate pass-derived vs. direct tickets; omitted returns all."""
    url = reverse("api:list_tickets", kwargs={"event_id": event.pk})

    response = owner_client.get(url, {"source": "pass"})
    assert response.status_code == 200
    data = response.json()
    assert {item["id"] for item in data["results"]} == {str(pass_ticket.id)}
    assert data["results"][0]["series_pass"] == {
        "held_pass_id": str(held_pass.id),
        "series_pass_id": str(series_pass.id),
        "name": series_pass.name,
    }

    response = owner_client.get(url, {"source": "direct"})
    assert response.status_code == 200
    data = response.json()
    assert {item["id"] for item in data["results"]} == {str(plain_ticket.id)}
    assert data["results"][0]["series_pass"] is None

    response = owner_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert {item["id"] for item in data["results"]} == {str(pass_ticket.id), str(plain_ticket.id)}
