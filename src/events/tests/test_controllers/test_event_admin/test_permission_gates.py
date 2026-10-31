"""Permission gates on event-admin actions that reach beyond the base permission."""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import orjson
import pytest
from django.test import RequestFactory
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from common.authentication import OAuthPrincipal
from events.controllers.permissions import require_series_pass_link_access
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
from oauth.exceptions import InsufficientScopeError

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
    organization: Organization,
    organization_staff_user: RevelUser,
    event: Event,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """Staff with manage_event but not manage_tickets cannot cancel with refunds."""
    client = _staff_client(organization, organization_staff_user, manage_event=True, manage_tickets=False)
    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": Event.EventStatus.CANCELLED})

    with patch("events.tasks.refunds.refund_cancelled_event_tickets.delay") as mock_delay:
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(url, data={"refund_tickets": True}, content_type="application/json")

    assert response.status_code == 403, response.content
    event.refresh_from_db()
    assert event.status != Event.EventStatus.CANCELLED
    mock_delay.assert_not_called()


def test_cancel_with_refunds_allowed_with_manage_tickets(
    organization: Organization,
    organization_staff_user: RevelUser,
    event: Event,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """Staff holding both manage_event and manage_tickets can cancel with refunds."""
    client = _staff_client(organization, organization_staff_user, manage_event=True, manage_tickets=True)
    url = reverse("api:update_event_status", kwargs={"event_id": event.pk, "status": Event.EventStatus.CANCELLED})

    with patch("events.tasks.refunds.refund_cancelled_event_tickets.delay") as mock_delay:
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(url, data={"refund_tickets": True}, content_type="application/json")

    assert response.status_code == 200, response.content
    mock_delay.assert_called_once()


def test_cancellation_refund_preview_requires_manage_tickets(
    organization: Organization, organization_staff_user: RevelUser, event: Event
) -> None:
    """The preview exposes Stripe balance and refundable revenue, so manage_event alone is not enough."""
    client = _staff_client(organization, organization_staff_user, manage_event=True, manage_tickets=False)
    url = reverse("api:event_cancellation_refund_preview", kwargs={"event_id": event.pk})

    response = client.get(url)

    assert response.status_code == 403, response.content


def test_cancellation_refund_preview_allowed_with_manage_tickets(
    organization: Organization, organization_staff_user: RevelUser, event: Event
) -> None:
    """Staff holding manage_event and manage_tickets can read the preview."""
    client = _staff_client(organization, organization_staff_user, manage_event=True, manage_tickets=True)
    url = reverse("api:event_cancellation_refund_preview", kwargs={"event_id": event.pk})

    with patch("stripe.Balance.retrieve"):
        response = client.get(url)

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


def _create_event_payload(pass_for_event: SeriesPass, ticket_tier: TicketTier) -> dict[str, t.Any]:
    return {
        "name": f"New {uuid4().hex[:6]}",
        "start": (timezone.now() + timedelta(days=7)).isoformat(),
        "series_pass_links": [{"series_pass_id": str(pass_for_event.id), "tier_id": str(ticket_tier.id)}],
    }


def test_create_event_series_pass_links_requires_edit_event_series(
    organization: Organization,
    organization_staff_user: RevelUser,
    pass_for_event: SeriesPass,
    ticket_tier: TicketTier,
) -> None:
    """Staff with create_event but not edit_event_series cannot link series passes inline on create."""
    client = _staff_client(organization, organization_staff_user, create_event=True, edit_event_series=False)
    url = reverse("api:create_event", kwargs={"slug": organization.slug})

    response = client.post(
        url, data=orjson.dumps(_create_event_payload(pass_for_event, ticket_tier)), content_type="application/json"
    )

    assert response.status_code == 403, response.content
    assert not Event.objects.filter(organization=organization, name__startswith="New ").exists()


def test_create_event_with_series_pass_links_allowed_with_edit_event_series(
    organization: Organization,
    organization_staff_user: RevelUser,
    pass_for_event: SeriesPass,
    ticket_tier: TicketTier,
) -> None:
    """Staff holding create_event and edit_event_series pass the series-pass-link gate on create."""
    client = _staff_client(organization, organization_staff_user, create_event=True, edit_event_series=True)
    url = reverse("api:create_event", kwargs={"slug": organization.slug})

    response = client.post(
        url, data=orjson.dumps(_create_event_payload(pass_for_event, ticket_tier)), content_type="application/json"
    )

    # The gate must not refuse; the payload's own validity is outside this test's concern.
    assert response.status_code != 403, response.content


def test_edit_event_series_pass_links_allowed_with_edit_event_series(
    organization: Organization,
    organization_staff_user: RevelUser,
    event: Event,
    pass_for_event: SeriesPass,
    ticket_tier: TicketTier,
) -> None:
    """Staff holding edit_event and edit_event_series pass the gate on edit."""
    client = _staff_client(organization, organization_staff_user, edit_event=True, edit_event_series=True)
    url = reverse("api:edit_event", kwargs={"event_id": event.pk})
    payload = {
        "name": event.name,
        "series_pass_links": [{"series_pass_id": str(pass_for_event.id), "tier_id": str(ticket_tier.id)}],
    }

    response = client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200, response.content


def _app_request(user: RevelUser, scopes: frozenset[str]) -> t.Any:
    request = RequestFactory().get("/")
    request.user = user
    request.auth = OAuthPrincipal(client_id="c", scopes=scopes)  # type: ignore[attr-defined]
    return request


def test_series_pass_link_access_app_token_needs_both_scopes(organization: Organization) -> None:
    """An app token needs the scopes unlocking edit_event_series and manage_tickets (org:tickets)."""
    owner = organization.owner
    with pytest.raises(InsufficientScopeError):
        require_series_pass_link_access(_app_request(owner, frozenset({"org:read", "org:events"})), organization)
    with pytest.raises(InsufficientScopeError) as exc:
        require_series_pass_link_access(_app_request(owner, frozenset({"org:read"})), organization)
    assert exc.value.scope is not None


def test_series_pass_link_access_app_token_with_scopes_passes(organization: Organization) -> None:
    """With the scopes granted, the owner's org permission carries the app token through."""
    owner = organization.owner
    scopes = frozenset({"org:read", "org:events", "org:tickets"})
    require_series_pass_link_access(_app_request(owner, scopes), organization)  # no raise
