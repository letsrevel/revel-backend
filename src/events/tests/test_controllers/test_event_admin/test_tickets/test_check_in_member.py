"""Tests for the check-in endpoint accepting ``member:`` membership-card codes.

Fixtures modeled on ``test_check_in.py`` (same directory) and
``test_series_pass/test_checkin.py`` (URL helper pattern).
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    Event,
    EventSeries,
    HeldSeriesPass,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    SeriesPass,
    Ticket,
    TicketTier,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _open_check_in_window(event: Event) -> None:
    """Ensure the check-in window is open on the event for all tests in this module."""
    now = timezone.now()
    event.check_in_starts_at = now - timedelta(hours=1)
    event.check_in_ends_at = now + timedelta(hours=1)
    event.save(update_fields=["check_in_starts_at", "check_in_ends_at"])


@pytest.fixture
def member(organization_membership: OrganizationMember) -> OrganizationMember:
    """An ACTIVE member of ``organization`` (see root conftest ``organization_membership``)."""
    organization_membership.refresh_from_db()
    return organization_membership


def _check_in_url(event: Event, code: str) -> str:
    return reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "code": code})


# --- 1. Single ACTIVE ticket: fast-path check-in, kind == "checked_in" ---


def test_member_scan_single_active_ticket_checks_in(
    organization_owner_client: Client,
    event: Event,
    member: OrganizationMember,
    event_ticket_tier: TicketTier,
) -> None:
    ticket = Ticket.objects.create(
        event=event, user=member.user, tier=event_ticket_tier, guest_name=member.user.get_display_name()
    )

    url = _check_in_url(event, member.qr_payload)
    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["kind"] == "checked_in"
    assert data["id"] == str(ticket.id)
    assert data["status"] == Ticket.TicketStatus.CHECKED_IN

    ticket.refresh_from_db()
    assert ticket.status == Ticket.TicketStatus.CHECKED_IN
    assert ticket.checked_in_by is not None


# --- 2. Zero tickets, ACTIVE member: report-only, kind == "member" ---


def test_member_scan_zero_tickets_reports_member(
    organization_owner_client: Client, event: Event, member: OrganizationMember
) -> None:
    url = _check_in_url(event, member.qr_payload)
    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["kind"] == "member"
    assert data["member"]["status"] == OrganizationMember.MembershipStatus.ACTIVE
    assert data["tickets"] == []


# --- 3. Two tickets: report-only, neither is checked in ---


def test_member_scan_two_tickets_checks_in_neither(
    organization_owner_client: Client,
    event: Event,
    member: OrganizationMember,
    event_ticket_tier: TicketTier,
) -> None:
    t1 = Ticket.objects.create(event=event, user=member.user, tier=event_ticket_tier, guest_name="Guest One")
    t2 = Ticket.objects.create(event=event, user=member.user, tier=event_ticket_tier, guest_name="Guest Two")

    url = _check_in_url(event, member.qr_payload)
    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["kind"] == "member"
    assert len(data["tickets"]) == 2
    assert {tk["id"] for tk in data["tickets"]} == {str(t1.id), str(t2.id)}

    t1.refresh_from_db()
    t2.refresh_from_db()
    assert t1.status != Ticket.TicketStatus.CHECKED_IN
    assert t2.status != Ticket.TicketStatus.CHECKED_IN


# --- 4. BANNED member, zero tickets: reported, not raised ---


def test_member_scan_banned_member_reports_status(
    organization_owner_client: Client, event: Event, member: OrganizationMember
) -> None:
    member.status = OrganizationMember.MembershipStatus.BANNED
    member.save(update_fields=["status"])

    url = _check_in_url(event, member.qr_payload)
    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["kind"] == "member"
    assert data["member"]["status"] == OrganizationMember.MembershipStatus.BANNED


# --- 5. Member of a different org: 404 ---


@pytest.fixture
def other_org(revel_user_factory: RevelUserFactory) -> Organization:
    owner = revel_user_factory(username="other_org_owner_ci")
    return Organization.objects.create(name="Other Org CI", slug="other-org-ci", owner=owner)


@pytest.fixture
def other_org_member(other_org: Organization, revel_user_factory: RevelUserFactory) -> OrganizationMember:
    user = revel_user_factory(username="other_org_member_ci")
    return OrganizationMember.objects.create(organization=other_org, user=user)


def test_member_scan_cross_org_member_404s(
    organization_owner_client: Client, event: Event, other_org_member: OrganizationMember
) -> None:
    url = _check_in_url(event, other_org_member.qr_payload)
    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 404


# --- 6. Malformed member code: 422 (path pattern rejects it before the view runs) ---


def test_member_scan_malformed_uuid_422(organization_owner_client: Client, event: Event) -> None:
    url = _check_in_url(event, "member:not-a-uuid")
    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 422


# --- 7. Regression: bare ticket UUID still checks in, kind == "checked_in" ---


def test_check_in_bare_ticket_uuid_regression(
    organization_owner_client: Client, event: Event, active_online_ticket: Ticket
) -> None:
    url = _check_in_url(event, str(active_online_ticket.id))
    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["kind"] == "checked_in"
    assert data["id"] == str(active_online_ticket.id)


# --- 8. Regression: series:<uuid> still checks in ---


def test_check_in_series_pass_regression(
    organization_owner_client: Client, event: Event, event_series: EventSeries, member_user: RevelUser
) -> None:
    series_pass = SeriesPass.objects.create(
        event_series=event_series,
        name="Season Ticket",
        price=Decimal("36.00"),
        pro_rata_discount=Decimal("6.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
    )
    held_pass = HeldSeriesPass.objects.create(
        series_pass=series_pass,
        user=member_user,
        status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
        price_paid=Decimal("36.00"),
    )
    pass_tier = TicketTier.objects.create(
        event=event, name="Pass Tier", price=Decimal("0"), payment_method=TicketTier.PaymentMethod.FREE
    )
    pass_ticket = Ticket.objects.create(
        event=event, user=member_user, tier=pass_tier, held_pass=held_pass, guest_name="Pass Holder"
    )

    url = _check_in_url(event, held_pass.qr_payload)
    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["kind"] == "checked_in"
    assert data["id"] == str(pass_ticket.id)

    pass_ticket.refresh_from_db()
    assert pass_ticket.status == Ticket.TicketStatus.CHECKED_IN


# --- 9. Staff without check_in_attendees permission: 403 ---


def test_member_scan_staff_without_permission_403(
    organization_staff_client: Client, event: Event, staff_member: OrganizationStaff, member: OrganizationMember
) -> None:
    perms = staff_member.permissions
    perms["default"]["check_in_attendees"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = _check_in_url(event, member.qr_payload)
    response = organization_staff_client.post(url, content_type="application/json")

    assert response.status_code == 403


# --- 10. price_paid body + member: code with single PWYC offline ticket: 200 checked_in ---


def test_member_scan_price_paid_pwyc_offline_checks_in(
    organization_owner_client: Client,
    event: Event,
    member: OrganizationMember,
    pwyc_offline_tier: TicketTier,
) -> None:
    ticket = Ticket.objects.create(
        event=event,
        user=member.user,
        tier=pwyc_offline_tier,
        status=Ticket.TicketStatus.PENDING,
        guest_name=member.user.get_display_name(),
    )

    url = _check_in_url(event, member.qr_payload)
    response = organization_owner_client.post(url, data={"price_paid": "12.50"}, content_type="application/json")

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["kind"] == "checked_in"
    assert data["status"] == Ticket.TicketStatus.CHECKED_IN
    assert data["price_paid"] == "12.50"

    ticket.refresh_from_db()
    assert ticket.status == Ticket.TicketStatus.CHECKED_IN
    assert ticket.price_paid == Decimal("12.50")
