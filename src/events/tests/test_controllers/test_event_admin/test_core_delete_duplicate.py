"""Tests for event admin core endpoints (delete, duplicate, discount give-back)."""

import typing as t

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Event, EventRSVP, OrganizationStaff, Ticket, TicketTier

pytestmark = pytest.mark.django_db


# --- Tests for DELETE /event-admin/{event_id} ---


def test_delete_event_by_owner(organization_owner_client: Client, event: Event) -> None:
    """Test that an event's organization owner can successfully delete it."""
    event_id = event.pk
    url = reverse("api:delete_event", kwargs={"event_id": event_id})

    response = organization_owner_client.delete(url)

    assert response.status_code == 204
    assert not Event.objects.filter(pk=event_id).exists()


def test_delete_event_with_tickets_survives_post_commit_waitlist_hook(
    organization_owner_client: Client, ticket: Ticket, django_capture_on_commit_callbacks: t.Any
) -> None:
    """Deleting an event cascades its tickets; the ticket post_delete waitlist hook must not 500 on commit.

    pytest-django rolls the wrapping transaction back so on_commit callbacks never fire on their own;
    django_capture_on_commit_callbacks(execute=True) runs them, mirroring ATOMIC_REQUESTS in production.
    Regression test for #937.
    """
    event_id = ticket.event_id
    url = reverse("api:delete_event", kwargs={"event_id": event_id})

    with django_capture_on_commit_callbacks(execute=True):
        response = organization_owner_client.delete(url)

    assert response.status_code == 204
    assert not Event.objects.filter(pk=event_id).exists()


def test_delete_event_with_rsvps_survives_post_commit_waitlist_hook(
    organization_owner_client: Client, event: Event, member_user: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    """Same as above for RSVP events: the EventRSVP post_delete hook shares the helper (#937)."""
    EventRSVP.objects.create(event=event, user=member_user, status=EventRSVP.RsvpStatus.YES)
    url = reverse("api:delete_event", kwargs={"event_id": event.pk})

    with django_capture_on_commit_callbacks(execute=True):
        response = organization_owner_client.delete(url)

    assert response.status_code == 204
    assert not Event.objects.filter(pk=event.pk).exists()


def test_delete_event_by_staff_with_permission(
    organization_staff_client: Client, event: Event, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member with 'delete_event' permission can delete."""
    # Grant delete_event permission
    perms = staff_member.permissions
    perms["default"]["delete_event"] = True
    staff_member.permissions = perms
    staff_member.save()

    event_id = event.pk
    url = reverse("api:delete_event", kwargs={"event_id": event_id})

    response = organization_staff_client.delete(url)

    assert response.status_code == 204
    assert not Event.objects.filter(pk=event_id).exists()


def test_delete_event_by_staff_without_permission(
    organization_staff_client: Client, event: Event, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member without 'delete_event' permission gets a 403."""
    # Remove the 'delete_event' permission
    perms = staff_member.permissions
    perms["default"]["delete_event"] = False
    staff_member.permissions = perms
    staff_member.save()

    event_id = event.pk
    url = reverse("api:delete_event", kwargs={"event_id": event_id})
    response = organization_staff_client.delete(url)

    assert response.status_code == 403
    # Event should still exist
    assert Event.objects.filter(pk=event_id).exists()


@pytest.mark.parametrize(
    "client_fixture,expected_status_code", [("member_client", 403), ("nonmember_client", 403), ("client", 401)]
)
def test_delete_event_by_unauthorized_users(
    request: pytest.FixtureRequest, client_fixture: str, expected_status_code: int, public_event: Event
) -> None:
    """Test that users without owner/staff roles get a 403/401 when trying to delete."""
    client: Client = request.getfixturevalue(client_fixture)
    event_id = public_event.pk
    url = reverse("api:delete_event", kwargs={"event_id": event_id})

    response = client.delete(url)

    assert response.status_code == expected_status_code
    # Event should still exist
    assert Event.objects.filter(pk=event_id).exists()


# --- Tests for POST /event-admin/{event_id}/duplicate ---


def test_duplicate_event_by_owner(organization_owner_client: Client, event: Event) -> None:
    """Test that an event's organization owner can duplicate it."""
    from datetime import timedelta

    new_start = event.start + timedelta(days=7)
    url = reverse("api:duplicate_event", kwargs={"event_id": event.pk})
    payload = {
        "name": "Duplicated Event",
        "start": new_start.isoformat(),
    }

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["name"] == "Duplicated Event"
    assert data["status"] == Event.EventStatus.DRAFT
    assert data["organization"]["id"] == str(event.organization.id)

    # Verify the new event exists
    new_event = Event.objects.get(pk=data["id"])
    assert new_event.name == "Duplicated Event"
    assert new_event.organization == event.organization


def test_duplicate_event_by_staff_with_permission(
    organization_staff_client: Client, event: Event, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member with 'create_event' permission can duplicate."""
    from datetime import timedelta

    # Grant create_event permission
    perms = staff_member.permissions
    perms["default"]["create_event"] = True
    staff_member.permissions = perms
    staff_member.save()

    new_start = event.start + timedelta(days=14)
    url = reverse("api:duplicate_event", kwargs={"event_id": event.pk})
    payload = {
        "name": "Staff Duplicated Event",
        "start": new_start.isoformat(),
    }

    response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["name"] == "Staff Duplicated Event"


def test_duplicate_event_by_staff_without_permission(
    organization_staff_client: Client, event: Event, staff_member: OrganizationStaff
) -> None:
    """Test that a staff member without 'create_event' permission gets 403."""
    from datetime import timedelta

    # Ensure permission is False
    perms = staff_member.permissions
    perms["default"]["create_event"] = False
    staff_member.permissions = perms
    staff_member.save()

    new_start = event.start + timedelta(days=7)
    url = reverse("api:duplicate_event", kwargs={"event_id": event.pk})
    payload = {
        "name": "Should Fail",
        "start": new_start.isoformat(),
    }

    response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 403


@pytest.mark.parametrize(
    "client_fixture,expected_status_code", [("member_client", 403), ("nonmember_client", 403), ("client", 401)]
)
def test_duplicate_event_by_unauthorized_users(
    request: pytest.FixtureRequest, client_fixture: str, expected_status_code: int, public_event: Event
) -> None:
    """Test that users without owner/staff roles get a 403/401 when trying to duplicate."""
    from datetime import timedelta

    client: Client = request.getfixturevalue(client_fixture)
    new_start = public_event.start + timedelta(days=7)
    url = reverse("api:duplicate_event", kwargs={"event_id": public_event.pk})
    payload = {
        "name": "Unauthorized Duplicate",
        "start": new_start.isoformat(),
    }

    response = client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == expected_status_code


def test_duplicate_event_preserves_ticket_tiers(
    organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier
) -> None:
    """Test that duplicating an event also duplicates its ticket tiers."""
    from datetime import timedelta

    new_start = event.start + timedelta(days=7)
    url = reverse("api:duplicate_event", kwargs={"event_id": event.pk})
    payload = {
        "name": "Event With Tiers",
        "start": new_start.isoformat(),
    }

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200, response.content
    data = response.json()

    # Verify ticket tiers were copied
    new_event = Event.objects.get(pk=data["id"])
    new_tiers = list(new_event.ticket_tiers.all())

    # Should have the default tier plus our event_ticket_tier copy
    assert len(new_tiers) >= 1

    # Find the copied tier (by name match)
    copied_tier = next((t for t in new_tiers if t.name == event_ticket_tier.name), None)
    assert copied_tier is not None
    assert copied_tier.price == event_ticket_tier.price
    assert copied_tier.quantity_sold == 0  # Should be reset


def test_duplicate_event_shifts_dates(organization_owner_client: Client, event: Event) -> None:
    """Test that duplicating an event shifts all date fields correctly."""
    from datetime import timedelta

    # Set up various date fields
    event.rsvp_before = event.start - timedelta(days=1)
    event.check_in_starts_at = event.start - timedelta(hours=1)
    event.check_in_ends_at = event.end + timedelta(hours=1)
    event.save()

    delta = timedelta(days=7)
    new_start = event.start + delta
    url = reverse("api:duplicate_event", kwargs={"event_id": event.pk})
    payload = {
        "name": "Date-Shifted Event",
        "start": new_start.isoformat(),
    }

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200, response.content
    data = response.json()

    # Verify date fields were shifted
    new_event = Event.objects.get(pk=data["id"])
    assert new_event.start == new_start
    assert new_event.end == event.end + delta
    assert new_event.rsvp_before == event.rsvp_before + delta
    assert new_event.check_in_starts_at == event.check_in_starts_at + delta
    assert new_event.check_in_ends_at == event.check_in_ends_at + delta


def test_duplicate_event_nonexistent(organization_owner_client: Client) -> None:
    """Test duplicating a non-existent event returns 404."""
    from datetime import timedelta
    from uuid import uuid4

    from django.utils import timezone

    fake_event_id = uuid4()
    new_start = timezone.now() + timedelta(days=30)
    url = reverse("api:duplicate_event", kwargs={"event_id": fake_event_id})
    payload = {
        "name": "Should Not Work",
        "start": new_start.isoformat(),
    }

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 404


def test_duplicate_event_requires_authentication(event: Event) -> None:
    """Test that duplicating an event requires authentication."""
    from datetime import timedelta

    new_start = event.start + timedelta(days=7)
    client = Client()
    url = reverse("api:duplicate_event", kwargs={"event_id": event.pk})
    payload = {
        "name": "Unauthenticated",
        "start": new_start.isoformat(),
    }

    response = client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 401


def test_delete_event_releases_pending_discount_uses(
    organization_owner_client: Client, event: Event, organization_owner_user: RevelUser
) -> None:
    """Deleting an event releases its PENDING tickets' uses (not ACTIVE ones, nor another event's)."""
    from events.models.discount_code import DiscountCode

    tier = TicketTier.objects.create(event=event, name="Offline", price="10.00", total_quantity=10)
    code = DiscountCode.objects.create(
        code="DELCODE",
        organization=event.organization,
        discount_type=DiscountCode.DiscountType.PERCENTAGE,
        discount_value="5.00",
        max_uses=5,
        is_active=True,
        times_used=3,
    )
    other_event = Event.objects.create(
        organization=event.organization, name="Other", slug="other-del", start=event.start
    )
    other_tier = TicketTier.objects.create(event=other_event, name="Offline", price="10.00", total_quantity=10)
    confirmed_user = RevelUser.objects.create_user(username="del_confirmed", email="del_confirmed@example.com")
    pending, active = Ticket.TicketStatus.PENDING, Ticket.TicketStatus.ACTIVE
    for ev, tr, usr, st in [
        (event, tier, organization_owner_user, pending),
        (event, tier, confirmed_user, active),
        (other_event, other_tier, confirmed_user, pending),
    ]:
        Ticket.objects.create(event=ev, tier=tr, user=usr, status=st, discount_code=code)

    response = organization_owner_client.delete(reverse("api:delete_event", kwargs={"event_id": event.pk}))

    assert response.status_code == 204
    code.refresh_from_db()
    assert code.times_used == 2  # only the target event's PENDING ticket is released
