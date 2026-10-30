"""Permission gates on event-admin actions that reach beyond the base permission."""

from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    Event,
    EventSeries,
    Organization,
    OrganizationStaff,
    PermissionMap,
    PermissionsSchema,
    SeriesPass,
    SeriesPassTierLink,
    TicketTier,
)

pytestmark = pytest.mark.django_db


def _staff_client(organization: Organization, user: RevelUser, **perms: bool) -> Client:
    OrganizationStaff.objects.create(
        organization=organization,
        user=user,
        permissions=PermissionsSchema(default=PermissionMap(**perms)).model_dump(mode="json"),
    )
    refresh = RefreshToken.for_user(user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token!s}")  # type: ignore[attr-defined]


def test_cancel_with_refunds_requires_manage_tickets(
    organization: Organization, organization_staff_user: RevelUser, event: Event
) -> None:
    """Staff with manage_event but not manage_tickets cannot cancel with refunds."""
    client = _staff_client(organization, organization_staff_user, manage_event=True, manage_tickets=False)
    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": Event.EventStatus.CANCELLED})

    with patch("events.tasks.refunds.refund_cancelled_event_tickets") as mock_task:
        response = client.post(url, data={"refund_tickets": True}, content_type="application/json")

    assert response.status_code == 403, response.content
    event.refresh_from_db()
    assert event.status != Event.EventStatus.CANCELLED
    mock_task.delay.assert_not_called()


def test_cancel_with_refunds_allowed_with_manage_tickets(
    organization: Organization, organization_staff_user: RevelUser, event: Event
) -> None:
    """Staff holding both manage_event and manage_tickets can cancel with refunds."""
    client = _staff_client(organization, organization_staff_user, manage_event=True, manage_tickets=True)
    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": Event.EventStatus.CANCELLED})

    response = client.post(url, data={"refund_tickets": True}, content_type="application/json")

    assert response.status_code == 200, response.content


@pytest.fixture
def ticket_tier(event: Event) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name="Series Tier",
        price=Decimal("10.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )


@pytest.fixture
def pass_for_event(event_series: EventSeries) -> SeriesPass:
    return SeriesPass.objects.create(
        event_series=event_series,
        name="Pass",
        price=Decimal("36.00"),
        pro_rata_discount=Decimal("6.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
    )


def test_edit_event_series_pass_links_requires_edit_event_series(
    organization: Organization,
    organization_staff_user: RevelUser,
    event: Event,
    pass_for_event: SeriesPass,
    ticket_tier: TicketTier,
) -> None:
    """Staff with edit_event but not edit_event_series cannot link series passes inline."""
    client = _staff_client(organization, organization_staff_user, edit_event=True, edit_event_series=False)
    url = reverse("api:edit_event", kwargs={"event_id": event.pk})
    payload = {
        "name": event.name,
        "series_pass_links": [{"series_pass_id": str(pass_for_event.id), "tier_id": str(ticket_tier.id)}],
    }

    response = client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 403, response.content
    assert not SeriesPassTierLink.objects.filter(series_pass=pass_for_event).exists()


def test_edit_event_without_series_pass_links_needs_only_edit_event(
    organization: Organization, organization_staff_user: RevelUser, event: Event
) -> None:
    """A plain edit (no series_pass_links) is unaffected by the extra gate."""
    client = _staff_client(organization, organization_staff_user, edit_event=True, edit_event_series=False)
    url = reverse("api:edit_event", kwargs={"event_id": event.pk})

    response = client.put(
        url, data=orjson.dumps({"name": f"Renamed {uuid4().hex[:6]}"}), content_type="application/json"
    )

    assert response.status_code == 200, response.content
