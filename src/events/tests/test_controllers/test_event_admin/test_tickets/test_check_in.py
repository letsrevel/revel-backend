"""Tests for event check-in window and check-in process."""

import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from events.models import Event, OrganizationStaff, Ticket

pytestmark = pytest.mark.django_db


# --- Tests for Event Check-in Window and Check-in Process ---


def test_update_event_check_in_window(organization_owner_client: Client, event: Event) -> None:
    """Test updating event with check-in window fields."""
    from datetime import timedelta

    check_in_start = event.start + timedelta(hours=-1)
    check_in_end = event.end + timedelta(hours=1)

    url = reverse("api:edit_event", kwargs={"event_id": event.pk})
    payload = {
        "check_in_starts_at": check_in_start.isoformat(),
        "check_in_ends_at": check_in_end.isoformat(),
    }

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    event.refresh_from_db()
    assert event.check_in_starts_at == check_in_start
    assert event.check_in_ends_at == check_in_end


def test_check_in_success(organization_owner_client: Client, event: Event, active_online_ticket: Ticket) -> None:
    """Test successful ticket check-in."""
    from datetime import timedelta

    from django.utils import timezone

    # Set check-in window to be open
    now = timezone.now()
    event.check_in_starts_at = now - timedelta(hours=1)
    event.check_in_ends_at = now + timedelta(hours=1)
    event.save()

    url = reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "ticket_id": active_online_ticket.pk})

    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(active_online_ticket.id)
    assert data["status"] == Ticket.TicketStatus.CHECKED_IN
    assert data["checked_in_at"] is not None

    active_online_ticket.refresh_from_db()
    assert active_online_ticket.status == Ticket.TicketStatus.CHECKED_IN
    assert active_online_ticket.checked_in_at is not None
    assert active_online_ticket.checked_in_by is not None


def test_check_in_already_checked_in(
    organization_owner_client: Client, event: Event, active_online_ticket: Ticket
) -> None:
    """Test check-in fails when ticket is already checked in."""
    from datetime import timedelta

    from django.utils import timezone

    # Set check-in window to be open
    now = timezone.now()
    event.check_in_starts_at = now - timedelta(hours=1)
    event.check_in_ends_at = now + timedelta(hours=1)
    event.save()

    # Mark ticket as already checked in
    active_online_ticket.status = Ticket.TicketStatus.CHECKED_IN
    active_online_ticket.checked_in_at = now
    active_online_ticket.save()

    url = reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "ticket_id": active_online_ticket.pk})

    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 400
    assert "already been checked in" in response.json()["detail"]


def test_check_in_window_not_open(
    organization_owner_client: Client, event: Event, active_online_ticket: Ticket
) -> None:
    """Test check-in fails when check-in window is not open."""
    from datetime import timedelta

    from django.utils import timezone

    # Set check-in window to be closed (in the future)
    now = timezone.now()
    event.check_in_starts_at = now + timedelta(hours=1)
    event.check_in_ends_at = now + timedelta(hours=2)
    event.save()

    url = reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "ticket_id": active_online_ticket.pk})

    response = organization_owner_client.post(url, content_type="application/json")

    assert response.status_code == 400
    assert "Check-in is not currently open" in response.json()["detail"]


def test_check_in_staff_with_permission(
    organization_staff_client: Client, event: Event, staff_member: OrganizationStaff, active_online_ticket: Ticket
) -> None:
    """Test staff member with check_in_attendees permission can check in tickets."""
    from datetime import timedelta

    from django.utils import timezone

    # Grant permission
    perms = staff_member.permissions
    perms["default"]["check_in_attendees"] = True
    staff_member.permissions = perms
    staff_member.save()

    # Set check-in window to be open
    now = timezone.now()
    event.check_in_starts_at = now - timedelta(hours=1)
    event.check_in_ends_at = now + timedelta(hours=1)
    event.save()

    url = reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "ticket_id": active_online_ticket.pk})

    response = organization_staff_client.post(url, content_type="application/json")

    assert response.status_code == 200
    active_online_ticket.refresh_from_db()
    assert active_online_ticket.status == Ticket.TicketStatus.CHECKED_IN
    assert active_online_ticket.checked_in_by == staff_member.user


def test_check_in_staff_without_permission(
    organization_staff_client: Client, event: Event, staff_member: OrganizationStaff, active_online_ticket: Ticket
) -> None:
    """Test staff member without check_in_attendees permission gets 403."""
    # Ensure permission is False
    perms = staff_member.permissions
    perms["default"]["check_in_attendees"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "ticket_id": active_online_ticket.pk})

    response = organization_staff_client.post(url, content_type="application/json")

    assert response.status_code == 403


def test_check_in_requires_authentication(event: Event, active_online_ticket: Ticket) -> None:
    """Test check-in requires authentication."""
    from django.test.client import Client

    client = Client()
    url = reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "ticket_id": active_online_ticket.pk})

    response = client.post(url, content_type="application/json")

    assert response.status_code == 401
