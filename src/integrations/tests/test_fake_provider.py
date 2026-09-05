"""The FakeProvider's remote store behaves like a tiny platform, so service tests can rely on it."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from integrations.exceptions import ProviderError
from integrations.providers.base import ListingProvider, RemoteEvent, RemoteTicketClass, TokenSet
from integrations.schema import IntegrationErrorCode
from integrations.tests.fake_provider import FakeProvider

TOKEN = TokenSet(access_token="t")


def _event(name: str = "Fake Night") -> RemoteEvent:
    start = datetime(2026, 12, 1, 18, 0, tzinfo=UTC)
    return RemoteEvent(name=name, start=start, end=start + timedelta(hours=3), timezone="Europe/Vienna", currency="EUR")


def test_still_satisfies_protocol() -> None:
    assert isinstance(FakeProvider(), ListingProvider)


def test_create_update_publish_cancel_roundtrip() -> None:
    p = FakeProvider()
    ref = p.create_event(TOKEN, "acc-1", _event())
    assert ref.remote_id == "ev-1" and ref.status == "draft" and ref.url.endswith("/ev-1")
    assert p.get_event(TOKEN, "ev-1").name == "Fake Night"
    p.update_event(TOKEN, "ev-1", _event("Renamed"))
    assert p.get_event(TOKEN, "ev-1").name == "Renamed"
    p.publish_event(TOKEN, "ev-1")
    assert p.get_event(TOKEN, "ev-1").status == "live"
    p.cancel_event(TOKEN, "ev-1")
    assert p.get_event(TOKEN, "ev-1").status == "cancelled"
    assert [c[0] for c in p.calls] == [
        "create_event",
        "get_event",
        "update_event",
        "get_event",
        "publish_event",
        "get_event",
        "cancel_event",
        "get_event",
    ]


def test_ticket_class_upsert_delete_and_pause() -> None:
    p = FakeProvider()
    p.create_event(TOKEN, "acc-1", _event())
    tc = RemoteTicketClass(name="GA", price=Decimal("10"), currency="EUR", is_free=False, quantity_total=50)
    tc_id = p.upsert_ticket_class(TOKEN, "ev-1", tc)
    assert tc_id == "tc-1"
    assert p.upsert_ticket_class(TOKEN, "ev-1", tc.model_copy(update={"remote_id": "tc-1", "name": "GA+"})) == "tc-1"
    assert [c.name for c in p.get_event(TOKEN, "ev-1").ticket_classes] == ["GA+"]
    p.set_ticket_class_paused(TOKEN, "ev-1", "tc-1", True)
    assert p.get_event(TOKEN, "ev-1").ticket_classes[0].hidden is True
    p.delete_ticket_class(TOKEN, "ev-1", "tc-1")
    assert p.get_event(TOKEN, "ev-1").ticket_classes == []


def test_list_events_and_missing() -> None:
    p = FakeProvider()
    p.create_event(TOKEN, "acc-1", _event("A"))
    p.create_event(TOKEN, "acc-1", _event("B"))
    assert [s.remote_id for s in p.list_events(TOKEN, "acc-1")] == ["ev-1", "ev-2"]
    p.missing.add("ev-2")
    with pytest.raises(ProviderError) as exc:
        p.get_event(TOKEN, "ev-2")
    assert exc.value.code == IntegrationErrorCode.REMOTE_EVENT_MISSING


def test_fail_map_raises_for_named_method() -> None:
    p = FakeProvider()
    p.fail["create_event"] = ProviderError(IntegrationErrorCode.PROVIDER_REJECTED, "nope")
    with pytest.raises(ProviderError):
        p.create_event(TOKEN, "acc-1", _event())
