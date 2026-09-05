"""Eventbrite provider read/write methods over a recording transport."""

import json
import typing as t
from pathlib import Path

from integrations.providers.base import TokenSet
from integrations.tests.recorder import Recorder

FIXTURES = Path(__file__).parent / "fixtures" / "eventbrite"
TOKEN = TokenSet(access_token="TOK")


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
