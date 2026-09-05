"""Eventbrite provider read/write methods over a recording transport."""

import json
import typing as t
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from integrations.exceptions import ProviderError
from integrations.providers.base import RemoteEvent, RemoteTicketClass, RemoteVenue, TokenSet
from integrations.schema import IntegrationErrorCode
from integrations.tests.recorder import Recorder

FIXTURES = Path(__file__).parent / "fixtures" / "eventbrite"
TOKEN = TokenSet(access_token="TOK")
START = datetime(2026, 10, 4, 11, 4, 17, tzinfo=UTC)


def _fixture(name: str) -> dict[str, t.Any]:
    return t.cast(dict[str, t.Any], json.loads((FIXTURES / f"{name}.json").read_text()))


def test_list_events_queries_org_and_maps() -> None:
    rec = Recorder({("GET", "/v3/organizations/3012894655993/events/"): (200, _fixture("events_list"))})
    events = rec.provider().list_events(TOKEN, "3012894655993")
    assert [e.remote_id for e in events] == ["1999760883635"]
    q = dict(rec.requests[0].url.params)
    assert q["status"] == "draft,live,started" and q["order_by"] == "start_asc"


def test_get_event_expands_and_maps_ticket_classes() -> None:
    rec = Recorder({("GET", "/v3/events/1999760883635/"): (200, _fixture("event_get_expanded"))})
    ev = rec.provider().get_event(TOKEN, "1999760883635")
    assert dict(rec.requests[0].url.params)["expand"] == "venue,ticket_classes"
    assert ev.remote_id == "1999760883635" and isinstance(ev.ticket_classes, list)


def test_list_events_follows_pagination() -> None:
    event = _fixture("events_list")["events"][0]
    page1 = {
        "pagination": {
            "object_count": 2,
            "page_number": 1,
            "page_size": 1,
            "page_count": 2,
            "has_more_items": True,
            "continuation": "cont-token-1",
        },
        "events": [event],
    }
    page2 = {
        "pagination": {
            "object_count": 2,
            "page_number": 2,
            "page_size": 1,
            "page_count": 2,
            "has_more_items": False,
        },
        "events": [event],
    }
    calls: list[dict[str, t.Any]] = []

    import httpx

    from integrations.providers.eventbrite.provider import EventbriteProvider

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        calls.append(params)
        body = page2 if params.get("continuation") == "cont-token-1" else page1
        return httpx.Response(200, json=body)

    provider = EventbriteProvider(client_id="K", client_secret="S", transport=httpx.MockTransport(handler))
    events = provider.list_events(TOKEN, "3012894655993")
    assert [e.remote_id for e in events] == ["1999760883635", "1999760883635"]
    assert len(calls) == 2
    assert "continuation" not in calls[0]
    assert calls[1]["continuation"] == "cont-token-1"


def _remote_event(with_venue: bool) -> RemoteEvent:
    venue = (
        RemoteVenue(name="Hall", address="Stephansplatz 1", city="Wien", postal_code="1010", country="AT")
        if with_venue
        else None
    )
    return RemoteEvent(
        name="Spike",
        summary="S.",
        start=START,
        end=START + timedelta(hours=3),
        timezone="Europe/Vienna",
        currency="EUR",
        venue=venue,
    )


def test_create_event_creates_venue_first_and_returns_ref() -> None:
    rec = Recorder(
        {
            ("POST", "/v3/organizations/ORG/venues/"): (200, _fixture("venue_create")),
            ("POST", "/v3/organizations/ORG/events/"): (200, _fixture("event_create")),
        }
    )
    ref = rec.provider().create_event(TOKEN, "ORG", _remote_event(with_venue=True))
    assert (
        ref.remote_id == "1999760883635"
        and ref.status == "draft"
        and ref.url.startswith("https://www.eventbrite.com/e/")
    )
    assert [r.url.path for r in rec.requests] == ["/v3/organizations/ORG/venues/", "/v3/organizations/ORG/events/"]
    sent = json.loads(rec.requests[1].content)["event"]
    assert sent["venue_id"] == "299287124" and "description" not in sent


def test_create_virtual_event_skips_venue() -> None:
    rec = Recorder({("POST", "/v3/organizations/ORG/events/"): (200, _fixture("event_create"))})
    rec.provider().create_event(TOKEN, "ORG", _remote_event(with_venue=False))
    assert [r.url.path for r in rec.requests] == ["/v3/organizations/ORG/events/"]


def test_update_event_posts_to_event_path() -> None:
    rec = Recorder({("POST", "/v3/events/1999760883635/"): (200, _fixture("event_create"))})
    ref = rec.provider().update_event(TOKEN, "1999760883635", _remote_event(with_venue=False))
    assert ref.remote_id == "1999760883635"


def test_update_event_missing_maps_to_remote_event_missing() -> None:
    rec = Recorder(
        {
            ("POST", "/v3/events/gone/"): (
                404,
                {"error": "NOT_FOUND", "error_description": "The event you requested does not exist."},
            )
        }
    )
    with pytest.raises(ProviderError) as exc:
        rec.provider().update_event(TOKEN, "gone", _remote_event(with_venue=False))
    assert exc.value.code == IntegrationErrorCode.REMOTE_EVENT_MISSING


def test_update_event_with_venue_recreates_it_first() -> None:
    rec = Recorder(
        {
            ("GET", "/v3/events/EV/"): (200, _fixture("event_create")),
            ("POST", "/v3/organizations/3012894655993/venues/"): (200, _fixture("venue_create")),
            ("POST", "/v3/events/EV/"): (200, _fixture("event_create")),
        }
    )
    ref = rec.provider().update_event(TOKEN, "EV", _remote_event(with_venue=True))
    assert ref.remote_id == "1999760883635"
    assert [r.url.path for r in rec.requests] == [
        "/v3/events/EV/",
        "/v3/organizations/3012894655993/venues/",
        "/v3/events/EV/",
    ]
    sent = json.loads(rec.requests[2].content)["event"]
    assert sent["venue_id"] == "299287124"


def test_upsert_ticket_class_create_and_update() -> None:
    rec = Recorder(
        {
            ("POST", "/v3/events/EV/ticket_classes/"): (200, _fixture("ticket_class_create_paid")),
            ("POST", "/v3/events/EV/ticket_classes/3451654576/"): (200, _fixture("ticket_class_create_paid")),
        }
    )
    tc = RemoteTicketClass(name="Regular", price=Decimal("15"), currency="EUR", is_free=False, quantity_total=100)
    assert rec.provider().upsert_ticket_class(TOKEN, "EV", tc) == "3451654576"
    assert (
        rec.provider().upsert_ticket_class(TOKEN, "EV", tc.model_copy(update={"remote_id": "3451654576"}))
        == "3451654576"
    )
    assert json.loads(rec.requests[0].content)["ticket_class"]["cost"] == "EUR,1500"
    assert rec.requests[1].url.path.endswith("/3451654576/")


def test_delete_and_pause_ticket_class() -> None:
    rec = Recorder(
        {
            ("DELETE", "/v3/events/EV/ticket_classes/TC/"): (200, {"deleted": True}),
            ("POST", "/v3/events/EV/ticket_classes/TC/"): (200, _fixture("ticket_class_create_paid")),
        }
    )
    p = rec.provider()
    p.delete_ticket_class(TOKEN, "EV", "TC")
    p.set_ticket_class_paused(TOKEN, "EV", "TC", True)
    assert rec.requests[0].method == "DELETE"
    assert json.loads(rec.requests[1].content) == {"ticket_class": {"hidden": True}}


def test_delete_ticket_class_already_gone_is_success() -> None:
    rec = Recorder(
        {("DELETE", "/v3/events/EV/ticket_classes/TC/"): (404, {"error": "NOT_FOUND", "error_description": "gone"})}
    )
    rec.provider().delete_ticket_class(TOKEN, "EV", "TC")


def test_set_description_reads_version_then_posts() -> None:
    rec = Recorder(
        {
            ("GET", "/v3/events/EV/structured_content/"): (200, _fixture("structured_content_set")),
            ("POST", "/v3/events/EV/structured_content/2/"): (200, _fixture("structured_content_set")),
        }
    )
    rec.provider().set_description(TOKEN, "EV", "<p>x</p>")
    assert [r.url.path for r in rec.requests] == [
        "/v3/events/EV/structured_content/",
        "/v3/events/EV/structured_content/2/",
    ]
    assert json.loads(rec.requests[1].content)["modules"][0]["data"]["body"]["text"] == "<p>x</p>"


def test_set_description_defaults_to_version_1_when_none() -> None:
    rec = Recorder(
        {
            ("GET", "/v3/events/EV/structured_content/"): (404, {"error": "NOT_FOUND", "error_description": "none"}),
            ("POST", "/v3/events/EV/structured_content/1/"): (200, _fixture("structured_content_set")),
        }
    )
    rec.provider().set_description(TOKEN, "EV", "<p>x</p>")
    assert rec.requests[1].url.path.endswith("/structured_content/1/")


def test_publish_and_cancel() -> None:
    rec = Recorder(
        {
            ("POST", "/v3/events/EV/publish/"): (200, {"published": True}),
            ("POST", "/v3/events/EV/cancel/"): (200, {"canceled": True}),
        }
    )
    p = rec.provider()
    p.publish_event(TOKEN, "EV")
    p.cancel_event(TOKEN, "EV")
    assert [r.url.path for r in rec.requests] == ["/v3/events/EV/publish/", "/v3/events/EV/cancel/"]


def test_publish_rejected_carries_message() -> None:
    rec = Recorder(
        {
            ("POST", "/v3/events/EV/publish/"): (
                400,
                {"error": "ARGUMENTS_ERROR", "error_description": "Venue is required"},
            )
        }
    )
    with pytest.raises(ProviderError) as exc:
        rec.provider().publish_event(TOKEN, "EV")
    assert (
        exc.value.code == IntegrationErrorCode.PROVIDER_REJECTED and exc.value.provider_message == "Venue is required"
    )
