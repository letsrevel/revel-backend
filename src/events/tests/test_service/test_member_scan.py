"""Tests for ``scan_member_code``: report-only member-code scanning at the door.

Fixtures modeled on ``src/events/tests/test_series_pass/test_checkin.py``.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from django.http import Http404
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Event, Organization, OrganizationMember, Ticket, TicketTier
from events.service.member_scan_service import scan_member_code

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _open_check_in_window(event: Event) -> None:
    """Ensure the check-in window is open on the event for all tests in this module."""
    now = timezone.now()
    event.check_in_starts_at = now - timedelta(hours=1)
    event.check_in_ends_at = now + timedelta(hours=1)
    event.save(update_fields=["check_in_starts_at", "check_in_ends_at"])


@pytest.fixture
def staff_user(organization_owner_user: RevelUser) -> RevelUser:
    """The user performing the scan (door staff / organizer)."""
    return organization_owner_user


@pytest.fixture
def member(organization_membership: OrganizationMember) -> OrganizationMember:
    """An ACTIVE member of ``organization`` (see root conftest ``organization_membership``)."""
    organization_membership.refresh_from_db()
    return organization_membership


def make_ticket(
    event: Event,
    user: RevelUser,
    *,
    status: Ticket.TicketStatus = Ticket.TicketStatus.ACTIVE,
    tier: TicketTier | None = None,
) -> Ticket:
    """Create a ticket for ``user`` at ``event``, auto-creating a plain online tier if none is given."""
    if tier is None:
        tier = TicketTier.objects.create(
            event=event,
            name=f"Tier {uuid4().hex[:6]}",
            price=Decimal("10.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.ONLINE,
        )
    return Ticket.objects.create(
        event=event,
        user=user,
        tier=tier,
        status=status,
        guest_name=user.get_display_name(),
    )


# --- 1. Single ACTIVE ticket: fast-path check-in ---


def test_single_active_ticket_checks_in(event: Event, member: OrganizationMember, staff_user: RevelUser) -> None:
    ticket = make_ticket(event, member.user, status=Ticket.TicketStatus.ACTIVE)

    result = scan_member_code(event, member.qr_payload, staff_user)

    assert result.checked_in is not None
    assert result.checked_in.id == ticket.id
    assert result.checked_in.status == Ticket.TicketStatus.CHECKED_IN
    assert result.tickets == [ticket] or [t.id for t in result.tickets] == [ticket.id]
    assert result.member.id == member.id

    ticket.refresh_from_db()
    assert ticket.status == Ticket.TicketStatus.CHECKED_IN


# --- 2. Zero tickets ---


def test_zero_tickets_reports_member_with_no_check_in(
    event: Event, member: OrganizationMember, staff_user: RevelUser
) -> None:
    result = scan_member_code(event, member.qr_payload, staff_user)

    assert result.checked_in is None
    assert result.tickets == []
    assert result.member.id == member.id
    assert result.member.status == OrganizationMember.MembershipStatus.ACTIVE


# --- 3. Two ACTIVE tickets: report-only, neither is burned ---


def test_two_tickets_scan_checks_nothing_in(event: Event, member: OrganizationMember, staff_user: RevelUser) -> None:
    t1 = make_ticket(event, member.user, status=Ticket.TicketStatus.ACTIVE)
    t2 = make_ticket(event, member.user, status=Ticket.TicketStatus.ACTIVE)

    result = scan_member_code(event, member.qr_payload, staff_user)

    assert result.checked_in is None
    assert {tk.id for tk in result.tickets} == {t1.id, t2.id}
    t1.refresh_from_db()
    t2.refresh_from_db()
    assert t1.status == Ticket.TicketStatus.ACTIVE
    assert t2.status == Ticket.TicketStatus.ACTIVE


# --- 4. Only a CANCELLED ticket: treated as zero ---


def test_only_cancelled_ticket_treated_as_zero(event: Event, member: OrganizationMember, staff_user: RevelUser) -> None:
    make_ticket(event, member.user, status=Ticket.TicketStatus.CANCELLED)

    result = scan_member_code(event, member.qr_payload, staff_user)

    assert result.checked_in is None
    assert result.tickets == []


# --- 5. Already-CHECKED_IN ticket: delegated 400 surfaces verbatim ---


def test_already_checked_in_ticket_raises_400(event: Event, member: OrganizationMember, staff_user: RevelUser) -> None:
    make_ticket(event, member.user, status=Ticket.TicketStatus.CHECKED_IN)

    with pytest.raises(HttpError) as exc_info:
        scan_member_code(event, member.qr_payload, staff_user)

    assert exc_info.value.status_code == 400
    assert "already been checked in" in str(exc_info.value)


# --- 6. Member of a different org than event.organization: 404 ---


@pytest.fixture
def other_org(revel_user_factory: RevelUserFactory) -> Organization:
    owner = revel_user_factory(username="other_org_owner")
    return Organization.objects.create(name="Other Org", slug="other-org", owner=owner)


@pytest.fixture
def other_org_member(other_org: Organization, revel_user_factory: RevelUserFactory) -> OrganizationMember:
    user = revel_user_factory(username="other_org_member")
    return OrganizationMember.objects.create(organization=other_org, user=user)


def test_member_of_different_org_404s(
    event: Event, other_org_member: OrganizationMember, staff_user: RevelUser
) -> None:
    with pytest.raises(Http404):
        scan_member_code(event, other_org_member.qr_payload, staff_user)


# --- 7. Unknown member uuid / malformed uuid after prefix: 404 ---


def test_unknown_member_uuid_404s(event: Event, staff_user: RevelUser) -> None:
    with pytest.raises(Http404):
        scan_member_code(event, f"{OrganizationMember.QR_PREFIX}{uuid4()}", staff_user)


def test_malformed_member_uuid_404s(event: Event, staff_user: RevelUser) -> None:
    with pytest.raises(Http404):
        scan_member_code(event, f"{OrganizationMember.QR_PREFIX}not-a-uuid", staff_user)


# --- 8. Non-ACTIVE member statuses with zero tickets: reported, not raised ---


@pytest.mark.parametrize(
    "status",
    [
        OrganizationMember.MembershipStatus.PAUSED,
        OrganizationMember.MembershipStatus.BANNED,
        OrganizationMember.MembershipStatus.CANCELLED,
    ],
)
def test_non_active_member_status_is_reported_not_raised(
    event: Event, member: OrganizationMember, staff_user: RevelUser, status: OrganizationMember.MembershipStatus
) -> None:
    member.status = status
    member.save(update_fields=["status"])

    result = scan_member_code(event, member.qr_payload, staff_user)

    assert result.checked_in is None
    assert result.tickets == []
    assert result.member.status == status


# --- 9. price_paid passes through to check_in_ticket for the single-PWYC-ticket case ---


def test_price_paid_passes_through_for_single_pwyc_ticket(
    event: Event, member: OrganizationMember, staff_user: RevelUser
) -> None:
    pwyc_tier = TicketTier.objects.create(
        event=event,
        name="PWYC Offline Tier",
        price=Decimal("0.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        price_type=TicketTier.PriceType.PWYC,
    )
    ticket = make_ticket(event, member.user, status=Ticket.TicketStatus.ACTIVE, tier=pwyc_tier)

    result = scan_member_code(event, member.qr_payload, staff_user, price_paid=Decimal("15.00"))

    assert result.checked_in is not None
    assert result.checked_in.id == ticket.id
    assert result.checked_in.price_paid == Decimal("15.00")
