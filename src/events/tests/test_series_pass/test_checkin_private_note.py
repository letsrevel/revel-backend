"""Series-pass check-in surfaces the org's private note about the holder (#1314)."""

from datetime import timedelta

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Event, HeldSeriesPass, OrganizationUserNote, SeriesPass, Ticket, TicketTier

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _open_check_in_window(event: Event) -> None:
    now = timezone.now()
    event.check_in_starts_at = now - timedelta(hours=1)
    event.check_in_ends_at = now + timedelta(hours=1)
    event.save(update_fields=["check_in_starts_at", "check_in_ends_at"])


@pytest.fixture
def owner_client(organization_owner_user: RevelUser) -> Client:
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
    return Ticket.objects.create(
        guest_name="Pass Holder",
        user=revel_user,
        event=event,
        tier=ticket_tier,
        status=Ticket.TicketStatus.ACTIVE,
        held_pass=held_pass,
    )


def _url(event: Event, code: str) -> str:
    return reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "code": code})


def test_series_pass_scan_includes_note(
    owner_client: Client, event: Event, pass_ticket: Ticket, held_pass: HeldSeriesPass, revel_user: RevelUser
) -> None:
    OrganizationUserNote.objects.create(organization=event.organization, user=revel_user, body="Needs step-free access")
    response = owner_client.post(_url(event, f"series:{held_pass.id}"), content_type="application/json")
    assert response.status_code == 200, response.content
    assert response.json()["private_note"] == "Needs step-free access"


def test_series_pass_scan_null_without_note(
    owner_client: Client, event: Event, pass_ticket: Ticket, held_pass: HeldSeriesPass
) -> None:
    response = owner_client.post(_url(event, f"series:{held_pass.id}"), content_type="application/json")
    assert response.status_code == 200, response.content
    assert response.json()["private_note"] is None
