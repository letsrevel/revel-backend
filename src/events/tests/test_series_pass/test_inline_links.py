"""Tests for inline ``series_pass_links`` on event create/update (events/service/event_update_service.py).

Ticket tiers can only be created for an event that already exists (``TicketTier.event``
is a non-nullable FK), so a brand-new event created in the same request can never already
own a tier of its own. The CREATE happy path below therefore verifies the *wiring*
(``series_pass_service.add_tier_links`` mocked, correct args) rather than a real
materialized link; the CREATE 404/400 cases and the UPDATE happy path exercise the real,
unmocked flow end-to-end.
"""

import typing as t
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Event, EventSeries, Organization, SeriesPass, SeriesPassTierLink, TicketTier

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner_client(organization_owner_user: RevelUser) -> Client:
    """A Client authenticated as the organization owner."""
    refresh = RefreshToken.for_user(organization_owner_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token!s}")  # type: ignore[attr-defined]


@pytest.fixture
def foreign_series_pass(foreign_series: EventSeries) -> SeriesPass:
    """A SeriesPass belonging to a different series than ``event_series``."""
    return SeriesPass.objects.create(
        event_series=foreign_series,
        name="Foreign Pass",
        price=Decimal("36.00"),
        pro_rata_discount=Decimal("6.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
    )


# --- Create ---


@patch("events.service.event_update_service.series_pass_service.add_tier_links")
def test_create_event_with_series_pass_links_wires_the_call(
    mock_add_tier_links: MagicMock,
    owner_client: Client,
    organization: Organization,
    event_series: EventSeries,
    series_pass: SeriesPass,
) -> None:
    """The create-event flow resolves the pass by (id, event_series) and calls add_tier_links.

    ``add_tier_links`` is mocked here because a genuinely fresh event can never already
    own a ticket tier (see module docstring) — this test isolates the wiring.
    """
    url = reverse("api:create_event", kwargs={"slug": organization.slug})
    tier_id = uuid4()
    payload = {
        "name": "New Series Event",
        "event_type": "public",
        "visibility": "public",
        "status": "open",
        "event_series_id": str(event_series.id),
        "start": timezone.now().timestamp(),
        "series_pass_links": [{"series_pass_id": str(series_pass.id), "tier_id": str(tier_id)}],
    }

    response = owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    created_event_id = UUID(response.json()["id"])
    mock_add_tier_links.assert_called_once_with(series_pass, [{"event_id": created_event_id, "tier_id": tier_id}])


def test_create_event_with_series_pass_links_wrong_series_404(
    owner_client: Client,
    organization: Organization,
    event_series: EventSeries,
    foreign_series_pass: SeriesPass,
) -> None:
    """A series_pass_id belonging to a different series 404s before any tier validation."""
    url = reverse("api:create_event", kwargs={"slug": organization.slug})
    payload = {
        "name": "New Series Event",
        "event_type": "public",
        "visibility": "public",
        "status": "open",
        "event_series_id": str(event_series.id),
        "start": timezone.now().timestamp(),
        "series_pass_links": [{"series_pass_id": str(foreign_series_pass.id), "tier_id": str(uuid4())}],
    }

    response = owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 404
    assert not Event.objects.filter(name="New Series Event").exists()


def test_create_event_with_series_pass_links_tier_mismatch_400(
    owner_client: Client,
    organization: Organization,
    event_series: EventSeries,
    series_pass: SeriesPass,
    other_event_tier: TicketTier,
) -> None:
    """A tier that belongs to a different event fails model clean() with a 400."""
    url = reverse("api:create_event", kwargs={"slug": organization.slug})
    payload = {
        "name": "New Series Event",
        "event_type": "public",
        "visibility": "public",
        "status": "open",
        "event_series_id": str(event_series.id),
        "start": timezone.now().timestamp(),
        "series_pass_links": [{"series_pass_id": str(series_pass.id), "tier_id": str(other_event_tier.id)}],
    }

    response = owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 400
    assert not Event.objects.filter(name="New Series Event").exists()


def test_create_event_without_series_pass_links_is_a_no_op(owner_client: Client, organization: Organization) -> None:
    """Omitting series_pass_links is a plain create — regression guard."""
    url = reverse("api:create_event", kwargs={"slug": organization.slug})
    payload = {
        "name": "Plain Event",
        "event_type": "public",
        "visibility": "public",
        "status": "open",
        "start": timezone.now().timestamp(),
    }

    response = owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    assert Event.objects.filter(name="Plain Event").exists()
    assert SeriesPassTierLink.objects.count() == 0


# --- Update ---


def test_update_event_with_series_pass_links_happy_path_dispatches_materialization(
    django_capture_on_commit_callbacks: t.Any,
    owner_client: Client,
    event: Event,
    series_pass: SeriesPass,
    ticket_tier: TicketTier,
) -> None:
    """PATCHing an existing event's series_pass_links creates the link and dispatches materialization.

    Unlike create, ``event`` and ``ticket_tier`` already exist here, so this is a genuine
    end-to-end happy path (no mocking of add_tier_links itself).
    """
    url = reverse("api:edit_event", kwargs={"event_id": event.pk})
    payload = {
        "name": event.name,
        "series_pass_links": [{"series_pass_id": str(series_pass.id), "tier_id": str(ticket_tier.id)}],
    }

    with (
        patch("events.service.series_pass_service.materialize_series_pass_holders.delay") as mock_delay,
        django_capture_on_commit_callbacks(execute=True),
    ):
        response = owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    assert SeriesPassTierLink.objects.filter(series_pass=series_pass, event=event, tier=ticket_tier).exists()
    mock_delay.assert_called_once_with(str(series_pass.id), [str(event.id)])


def test_update_event_re_sending_existing_series_pass_link_is_a_noop_and_other_fields_apply(
    owner_client: Client,
    event: Event,
    series_pass: SeriesPass,
    ticket_tier: TicketTier,
) -> None:
    """PUTting an event's already-existing series_pass_links back is idempotent.

    Before the fix, re-sending the exact same, already-linked (series_pass, tier) pair
    hit the unique constraint as a ValidationError, 400ing the WHOLE PUT and rolling
    back unrelated field changes (e.g. a rename) submitted in the same request.
    """
    SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=ticket_tier)

    url = reverse("api:edit_event", kwargs={"event_id": event.pk})
    payload = {
        "name": "Renamed While Re-linking",
        "series_pass_links": [{"series_pass_id": str(series_pass.id), "tier_id": str(ticket_tier.id)}],
    }

    response = owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    event.refresh_from_db()
    assert event.name == "Renamed While Re-linking"
    assert SeriesPassTierLink.objects.filter(series_pass=series_pass, event=event).count() == 1


def test_update_event_with_series_pass_links_wrong_series_404(
    owner_client: Client, event: Event, foreign_series_pass: SeriesPass
) -> None:
    """A series_pass_id belonging to a different series 404s on update too."""
    url = reverse("api:edit_event", kwargs={"event_id": event.pk})
    payload = {
        "name": event.name,
        "series_pass_links": [{"series_pass_id": str(foreign_series_pass.id), "tier_id": str(uuid4())}],
    }

    response = owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 404
    assert SeriesPassTierLink.objects.count() == 0


def test_update_event_without_event_series_and_links_404(
    owner_client: Client, organization: Organization, series_pass: SeriesPass
) -> None:
    """An event with no event_series at all 404s when series_pass_links is provided.

    ``get_object_or_404(SeriesPass, pk=..., event_series_id=None)`` naturally never
    matches a real SeriesPass (they always belong to a series).
    """
    standalone_event = Event.objects.create(
        organization=organization,
        name="Standalone Event",
        slug="standalone-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        max_attendees=100,
        start=timezone.now(),
        status=Event.EventStatus.OPEN,
        requires_ticket=True,
    )
    assert standalone_event.event_series_id is None

    url = reverse("api:edit_event", kwargs={"event_id": standalone_event.pk})
    payload = {
        "name": standalone_event.name,
        "series_pass_links": [{"series_pass_id": str(series_pass.id), "tier_id": str(uuid4())}],
    }

    response = owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 404


def test_update_event_without_series_pass_links_is_a_no_op(owner_client: Client, event: Event) -> None:
    """Omitting series_pass_links on update is a plain edit — regression guard."""
    url = reverse("api:edit_event", kwargs={"event_id": event.pk})
    payload = {"name": "Renamed Event"}

    response = owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    event.refresh_from_db()
    assert event.name == "Renamed Event"
    assert SeriesPassTierLink.objects.count() == 0
