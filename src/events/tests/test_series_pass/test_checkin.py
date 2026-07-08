"""Tests for QR-agnostic check-in: series pass QR codes resolve to the holder's per-event ticket.

Covers both the service-level resolver (``resolve_check_in_ticket_id``) and the
``/tickets/{code}/check-in`` endpoint end-to-end.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.http import Http404
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja.errors import HttpError
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Event, EventSeries, HeldSeriesPass, SeriesPass, Ticket, TicketTier
from events.service.ticket_service import check_in_ticket, resolve_check_in_ticket_id

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _open_check_in_window(event: Event, other_event: Event) -> None:
    """Ensure the check-in window is open on both events for all tests in this module."""
    now = timezone.now()
    for evt in (event, other_event):
        evt.check_in_starts_at = now - timedelta(hours=1)
        evt.check_in_ends_at = now + timedelta(hours=1)
        evt.save(update_fields=["check_in_starts_at", "check_in_ends_at"])


@pytest.fixture
def owner_client(organization_owner_user: RevelUser) -> Client:
    """API client for the organization owner (bypasses check_in_attendees permission checks)."""
    refresh = RefreshToken.for_user(organization_owner_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def held_pass(series_pass: SeriesPass, revel_user: RevelUser) -> HeldSeriesPass:
    return HeldSeriesPass.objects.create(
        series_pass=series_pass,
        user=revel_user,
        status=HeldSeriesPass.Status.ACTIVE,
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


def _check_in_url(event: Event, code: str) -> str:
    return reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "code": code})


# --- Service-level resolver tests ---


def test_resolve_plain_ticket_uuid(event: Event, plain_ticket: Ticket) -> None:
    """A plain ticket UUID resolves to itself."""
    assert resolve_check_in_ticket_id(event, str(plain_ticket.id)) == plain_ticket.id


def test_resolve_series_qr_resolves_holder_ticket_for_event(
    event: Event, pass_ticket: Ticket, held_pass: HeldSeriesPass
) -> None:
    """A ``series:<uuid>`` code resolves to the holder's materialized ticket for this event."""
    assert resolve_check_in_ticket_id(event, f"series:{held_pass.id}") == pass_ticket.id


def test_resolve_series_qr_malformed_uuid_404(event: Event) -> None:
    """A malformed UUID after the ``series:`` prefix 404s (no ValueError leak)."""
    with pytest.raises(Http404):
        resolve_check_in_ticket_id(event, "series:not-a-uuid")


def test_resolve_series_qr_uncovered_event_404(
    other_event: Event, held_pass: HeldSeriesPass, pass_ticket: Ticket
) -> None:
    """A valid pass QR at an event the pass does not cover 404s."""
    with pytest.raises(Http404):
        resolve_check_in_ticket_id(other_event, f"series:{held_pass.id}")


def test_resolve_series_qr_cancelled_ticket_not_resolved_404(
    event: Event, pass_ticket: Ticket, held_pass: HeldSeriesPass
) -> None:
    """A cancelled pass ticket is excluded and 404s."""
    pass_ticket.status = Ticket.TicketStatus.CANCELLED
    pass_ticket.save(update_fields=["status"])

    with pytest.raises(Http404):
        resolve_check_in_ticket_id(event, f"series:{held_pass.id}")


def test_resolve_garbage_code_404(event: Event) -> None:
    """A plain garbage string (not a UUID, no prefix) 404s."""
    with pytest.raises(Http404):
        resolve_check_in_ticket_id(event, "not-a-uuid-or-prefix")


# --- Endpoint-level tests ---


def test_check_in_via_pass_qr(
    owner_client: Client, event: Event, pass_ticket: Ticket, held_pass: HeldSeriesPass
) -> None:
    """Scanning the pass QR checks in the holder's ticket for this event."""
    url = _check_in_url(event, f"series:{held_pass.id}")
    response = owner_client.post(url, content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(pass_ticket.id)
    assert data["status"] == Ticket.TicketStatus.CHECKED_IN

    pass_ticket.refresh_from_db()
    assert pass_ticket.status == Ticket.TicketStatus.CHECKED_IN
    assert pass_ticket.checked_in_at is not None


def test_check_in_via_plain_ticket_uuid_regression(owner_client: Client, event: Event, plain_ticket: Ticket) -> None:
    """A plain ticket UUID still checks in normally (regression)."""
    url = _check_in_url(event, str(plain_ticket.id))
    response = owner_client.post(url, content_type="application/json")

    assert response.status_code == 200
    plain_ticket.refresh_from_db()
    assert plain_ticket.status == Ticket.TicketStatus.CHECKED_IN


def test_check_in_series_qr_malformed_uuid_422_endpoint(owner_client: Client, event: Event) -> None:
    """A malformed pass code fails the path param's shape validation (not a 500).

    The ``code`` path param is now bounded/shaped (bare or ``series:``-prefixed canonical
    UUID) via ninja's ``Path(...)``, so garbage never reaches the view/resolver — it's
    rejected at request parsing with a 422, not the resolver's 404.
    """
    url = _check_in_url(event, "series:not-a-uuid")
    response = owner_client.post(url, content_type="application/json")

    assert response.status_code == 422


def test_check_in_pass_qr_uncovered_event_404_endpoint(
    owner_client: Client, other_event: Event, held_pass: HeldSeriesPass, pass_ticket: Ticket
) -> None:
    """A valid pass QR scanned at an event not covered by the pass 404s."""
    url = _check_in_url(other_event, f"series:{held_pass.id}")
    response = owner_client.post(url, content_type="application/json")

    assert response.status_code == 404


def test_check_in_cancelled_pass_ticket_404_endpoint(
    owner_client: Client, event: Event, pass_ticket: Ticket, held_pass: HeldSeriesPass
) -> None:
    """A cancelled pass ticket cannot be checked in via its pass QR."""
    pass_ticket.status = Ticket.TicketStatus.CANCELLED
    pass_ticket.save(update_fields=["status"])

    url = _check_in_url(event, f"series:{held_pass.id}")
    response = owner_client.post(url, content_type="application/json")

    assert response.status_code == 404


def test_check_in_garbage_code_422_endpoint(owner_client: Client, event: Event) -> None:
    """A plain garbage string (not UUID, no prefix) fails path param validation, 422."""
    url = _check_in_url(event, "not-a-uuid-or-prefix")
    response = owner_client.post(url, content_type="application/json")

    assert response.status_code == 422


# --- Pass-aware payment semantics at check-in ---
# The PASS's payment method is authoritative for PENDING pass tickets, not the
# mapped tier's; PWYC tier price semantics never apply to pass tickets.


def _make_pass(event_series: EventSeries, name: str, payment_method: str) -> SeriesPass:
    return SeriesPass.objects.create(
        event_series=event_series,
        name=name,
        price=Decimal("30.00"),
        pro_rata_discount=Decimal("5.00"),
        currency="EUR",
        payment_method=payment_method,
    )


def _make_held_pass(series_pass: SeriesPass, user: RevelUser, status: str) -> HeldSeriesPass:
    return HeldSeriesPass.objects.create(
        series_pass=series_pass, user=user, status=status, price_paid=series_pass.price
    )


class TestPassTicketCheckInPaymentSemantics:
    def test_pending_offline_pass_ticket_checks_in_even_on_online_tier(
        self,
        event: Event,
        event_series: EventSeries,
        ticket_tier: TicketTier,
        revel_user: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        """Offline pass, ONLINE mapped tier: payment collected at the door -> allowed."""
        offline_pass = _make_pass(event_series, "Offline Pass", TicketTier.PaymentMethod.OFFLINE)
        held = _make_held_pass(offline_pass, revel_user, HeldSeriesPass.Status.PENDING)
        ticket = Ticket.objects.create(
            event=event,
            tier=ticket_tier,  # ONLINE tier
            user=revel_user,
            held_pass=held,
            status=Ticket.TicketStatus.PENDING,
            guest_name="Pass Holder",
        )

        result = check_in_ticket(event, ticket.id, organization_owner_user)

        assert result.status == Ticket.TicketStatus.CHECKED_IN

    def test_pending_online_pass_ticket_rejected_even_on_offline_tier(
        self,
        event: Event,
        event_series: EventSeries,
        revel_user: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        """Online pass, OFFLINE mapped tier: payment must complete online first -> 400."""
        offline_tier = TicketTier.objects.create(
            event=event,
            name="Offline Mapped Tier",
            price=Decimal("10.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.OFFLINE,
        )
        online_pass = _make_pass(event_series, "Online Pass", TicketTier.PaymentMethod.ONLINE)
        held = _make_held_pass(online_pass, revel_user, HeldSeriesPass.Status.PENDING)
        ticket = Ticket.objects.create(
            event=event,
            tier=offline_tier,
            user=revel_user,
            held_pass=held,
            status=Ticket.TicketStatus.PENDING,
            guest_name="Pass Holder",
        )

        with pytest.raises(HttpError) as exc_info:
            check_in_ticket(event, ticket.id, organization_owner_user)
        assert exc_info.value.status_code == 400

        ticket.refresh_from_db()
        assert ticket.status == Ticket.TicketStatus.PENDING

    def test_pwyc_mapped_pass_ticket_demands_no_price(
        self,
        event: Event,
        event_series: EventSeries,
        revel_user: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        """A pass ticket on a PWYC offline tier needs no price_paid — the pass was paid."""
        pwyc_tier = TicketTier.objects.create(
            event=event,
            name="PWYC Tier",
            price=Decimal("0.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.OFFLINE,
            price_type=TicketTier.PriceType.PWYC,
        )
        free_pass = _make_pass(event_series, "PWYC-mapped Pass", TicketTier.PaymentMethod.FREE)
        held = _make_held_pass(free_pass, revel_user, HeldSeriesPass.Status.ACTIVE)
        ticket = Ticket.objects.create(
            event=event,
            tier=pwyc_tier,
            user=revel_user,
            held_pass=held,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name="Pass Holder",
        )

        result = check_in_ticket(event, ticket.id, organization_owner_user, price_paid=None)

        assert result.status == Ticket.TicketStatus.CHECKED_IN
        result.refresh_from_db()
        assert result.price_paid is None
