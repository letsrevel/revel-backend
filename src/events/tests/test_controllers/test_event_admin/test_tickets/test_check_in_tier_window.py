"""Tests for per-tier check-in windows (#994).

A tier's window is two optional offsets from the event start; each falls back
independently to the event's check-in window, then to the event start/end.
"""

import typing as t
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import orjson
import pytest
from django.contrib.gis.geos import Point
from django.core.exceptions import ValidationError
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_duration

from events.models import Event, Ticket, TicketTier
from events.service.duplication import duplicate_event
from geo.models import City

pytestmark = pytest.mark.django_db


def _check_in(client: Client, event: Event, ticket: Ticket) -> t.Any:
    url = reverse("api:check_in_ticket", kwargs={"event_id": event.pk, "code": ticket.pk})
    return client.post(url, content_type="application/json")


def _set_offsets(tier: TicketTier, opens: timedelta | None, closes: timedelta | None) -> None:
    tier.check_in_opens_offset = opens
    tier.check_in_closes_offset = closes
    tier.save()


# --- Check-in gate ---


def test_check_in_inside_tier_window(
    organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier, active_online_ticket: Ticket
) -> None:
    """The tier window overrides a closed event window."""
    event.check_in_starts_at = timezone.now() + timedelta(hours=5)
    event.check_in_ends_at = timezone.now() + timedelta(hours=6)
    event.save()
    _set_offsets(event_ticket_tier, timedelta(hours=-1), timedelta(hours=1))

    response = _check_in(organization_owner_client, event, active_online_ticket)

    assert response.status_code == 200, response.content
    active_online_ticket.refresh_from_db()
    assert active_online_ticket.status == Ticket.TicketStatus.CHECKED_IN


def test_check_in_before_tier_window_names_the_tier(
    organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier, active_online_ticket: Ticket
) -> None:
    _set_offsets(event_ticket_tier, timedelta(hours=1), timedelta(hours=2))

    response = _check_in(organization_owner_client, event, active_online_ticket)

    assert response.status_code == 400
    assert response.json()["detail"].startswith("Check-in for General is not open yet.")
    active_online_ticket.refresh_from_db()
    assert active_online_ticket.status == Ticket.TicketStatus.ACTIVE


def test_check_in_after_tier_window_names_the_tier(
    organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier, active_online_ticket: Ticket
) -> None:
    _set_offsets(event_ticket_tier, timedelta(hours=-2), timedelta(hours=-1))

    response = _check_in(organization_owner_client, event, active_online_ticket)

    assert response.status_code == 400
    assert response.json()["detail"].startswith("Check-in for General has closed.")


def test_only_opens_offset_set_closes_falls_back_to_event(
    organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier, active_online_ticket: Ticket
) -> None:
    """Closing falls back to the event's check-in end, which has already passed."""
    event.check_in_starts_at = timezone.now() - timedelta(hours=3)
    event.check_in_ends_at = timezone.now() - timedelta(minutes=30)
    event.save()
    _set_offsets(event_ticket_tier, timedelta(hours=-1), None)

    assert event_ticket_tier.effective_check_in_window()[1] == event.check_in_ends_at
    response = _check_in(organization_owner_client, event, active_online_ticket)

    assert response.status_code == 400
    assert response.json()["detail"].startswith("Check-in for General has closed.")


def test_only_closes_offset_set_opens_falls_back_to_event_start(
    organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier, active_online_ticket: Ticket
) -> None:
    """No event check-in window → opens at event.start (the fixture's "now"), closes at the tier offset."""
    _set_offsets(event_ticket_tier, None, timedelta(hours=1))

    opens_at, closes_at = event_ticket_tier.effective_check_in_window()
    assert opens_at == event.start
    assert closes_at == event.start + timedelta(hours=1)
    assert _check_in(organization_owner_client, event, active_online_ticket).status_code == 200


def test_no_offsets_uses_event_window_and_generic_message(
    organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier, active_online_ticket: Ticket
) -> None:
    event.check_in_starts_at = timezone.now() + timedelta(hours=1)
    event.check_in_ends_at = timezone.now() + timedelta(hours=2)
    event.save()

    assert event_ticket_tier.effective_check_in_window() == (event.check_in_starts_at, event.check_in_ends_at)
    response = _check_in(organization_owner_client, event, active_online_ticket)

    assert response.status_code == 400
    assert response.json()["detail"].startswith("Check-in is not open yet.")


def test_open_tier_window_still_requires_open_event(
    organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier, active_online_ticket: Ticket
) -> None:
    _set_offsets(event_ticket_tier, timedelta(hours=-1), timedelta(hours=1))
    event.status = Event.EventStatus.DRAFT
    event.save()

    response = _check_in(organization_owner_client, event, active_online_ticket)

    assert response.status_code == 400
    assert response.json()["detail"] == "Check-in is not currently open for this event."


# --- Validation ---


def test_clean_rejects_inverted_tier_offsets(event_ticket_tier: TicketTier) -> None:
    with pytest.raises(ValidationError) as exc:
        _set_offsets(event_ticket_tier, timedelta(hours=2), timedelta(hours=1))
    assert "check_in_closes_offset" in exc.value.message_dict


def test_clean_rejects_window_inverted_after_fallback(event: Event, event_ticket_tier: TicketTier) -> None:
    """Opening after the event's (fallback) check-in end is an empty window."""
    event.check_in_ends_at = event.start + timedelta(hours=2)
    event.save()
    with pytest.raises(ValidationError) as exc:
        _set_offsets(event_ticket_tier, timedelta(hours=3), None)
    assert "check_in_closes_offset" in exc.value.message_dict


# --- DST: offsets are wall-clock in the event's timezone ---


def test_offset_is_wall_clock_across_dst_change(event: Event, event_ticket_tier: TicketTier) -> None:
    """Vienna leaves CEST on 2026-10-25: "+1 day" from Saturday 10:00 is Sunday 10:00, not 09:00."""
    vienna = ZoneInfo("Europe/Vienna")
    event.city = City.objects.create(
        name="Vienna",
        ascii_name="Vienna",
        country="Austria",
        iso2="AT",
        iso3="AUT",
        city_id=2761369,
        location=Point(16.37, 48.21),
        population=1000,
        timezone="Europe/Vienna",
    )
    event.start = datetime(2026, 10, 24, 10, 0, tzinfo=vienna)
    event.end = datetime(2026, 10, 25, 23, 0, tzinfo=vienna)
    event.save()
    _set_offsets(event_ticket_tier, timedelta(days=1), timedelta(days=1, hours=8))

    opens_at, closes_at = event_ticket_tier.effective_check_in_window()

    assert opens_at == datetime(2026, 10, 25, 10, 0, tzinfo=vienna)
    assert opens_at.astimezone(ZoneInfo("UTC")).hour == 9  # CET = UTC+1
    assert closes_at == datetime(2026, 10, 25, 18, 0, tzinfo=vienna)


# --- API ---


def test_admin_sets_offsets_as_iso_durations(organization_owner_client: Client, event_ticket_tier: TicketTier) -> None:
    url = reverse(
        "api:update_ticket_tier", kwargs={"event_id": event_ticket_tier.event_id, "tier_id": event_ticket_tier.id}
    )
    payload = {"check_in_opens_offset": "-PT1H", "check_in_closes_offset": "P1DT2H"}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200, response.content
    event_ticket_tier.refresh_from_db()
    assert event_ticket_tier.check_in_opens_offset == timedelta(hours=-1)
    assert event_ticket_tier.check_in_closes_offset == timedelta(days=1, hours=2)


def test_admin_rejects_inverted_offsets(organization_owner_client: Client, event_ticket_tier: TicketTier) -> None:
    url = reverse(
        "api:update_ticket_tier", kwargs={"event_id": event_ticket_tier.event_id, "tier_id": event_ticket_tier.id}
    )
    payload = {"check_in_opens_offset": "PT2H", "check_in_closes_offset": "PT1H"}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 400


def test_public_tier_list_exposes_offsets_and_effective_window(
    organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier
) -> None:
    _set_offsets(event_ticket_tier, timedelta(hours=-1), None)

    response = organization_owner_client.get(reverse("api:tier_list", kwargs={"event_id": event.pk}))

    assert response.status_code == 200, response.content
    tier = next(t for t in response.json() if t["id"] == str(event_ticket_tier.id))
    # Ninja's DjangoJSONEncoder emits the long ISO 8601 form ("-P0DT01H00M00S").
    assert parse_duration(tier["check_in_opens_offset"]) == timedelta(hours=-1)
    assert tier["check_in_closes_offset"] is None
    # The JSON encoder truncates to milliseconds.
    opens_at = datetime.fromisoformat(tier["effective_check_in_opens_at"])
    closes_at = datetime.fromisoformat(tier["effective_check_in_closes_at"])
    assert abs(opens_at - (event.start - timedelta(hours=1))) < timedelta(milliseconds=1)
    assert abs(closes_at - event.end) < timedelta(milliseconds=1)


# --- Duplication ---


def test_duplication_copies_offsets_and_window_follows_new_start(event: Event, event_ticket_tier: TicketTier) -> None:
    _set_offsets(event_ticket_tier, timedelta(hours=-1), timedelta(hours=4))
    new_start = event.start + timedelta(days=7)

    new_event = duplicate_event(event, new_name="Next week", new_start=new_start)

    new_tier = new_event.ticket_tiers.get(name="General")
    assert new_tier.check_in_opens_offset == timedelta(hours=-1)
    assert new_tier.check_in_closes_offset == timedelta(hours=4)
    assert new_tier.effective_check_in_window() == (new_start - timedelta(hours=1), new_start + timedelta(hours=4))
