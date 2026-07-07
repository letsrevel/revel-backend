"""Tests for QR-agnostic check-in: series pass QR codes resolve to the holder's per-event ticket.

Covers both the service-level resolver (``resolve_check_in_ticket_id``) and the
``/tickets/{code}/check-in`` endpoint end-to-end.
"""

from datetime import timedelta

import pytest
from django.http import Http404
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Event, HeldSeriesPass, SeriesPass, Ticket, TicketTier
from events.service.ticket_service import resolve_check_in_ticket_id

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


def test_check_in_series_qr_malformed_uuid_404_endpoint(owner_client: Client, event: Event) -> None:
    """A malformed pass code 404s at the endpoint (not a 500)."""
    url = _check_in_url(event, "series:not-a-uuid")
    response = owner_client.post(url, content_type="application/json")

    assert response.status_code == 404


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


def test_check_in_garbage_code_404_endpoint(owner_client: Client, event: Event) -> None:
    """A plain garbage string (not UUID, no prefix) 404s at the endpoint."""
    url = _check_in_url(event, "not-a-uuid-or-prefix")
    response = owner_client.post(url, content_type="application/json")

    assert response.status_code == 404
