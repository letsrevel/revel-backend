"""Import: remote → Revel draft with tiers; link created last so auto-sync stays quiet; idempotent."""

import typing as t
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db.models import Manager

from events.models import Event, TicketTier
from integrations.models import EventLink, PlatformConnection, TierLink
from integrations.providers.base import RemoteEvent, RemoteTicketClass, RemoteVenue
from integrations.service import connection_service, sync_service
from integrations.tests.fake_provider import FakeProvider

pytestmark = pytest.mark.django_db
START = datetime(2026, 12, 1, 18, 0, tzinfo=UTC)


@contextmanager
def _force_first_lookup_miss(manager: Manager[t.Any]) -> t.Iterator[None]:
    """Make ``manager.filter(...)`` miss on its first call, then behave normally.

    Mirrors ``common/tests/test_utils.py::force_first_lookup_miss``. Simulates a concurrent
    importer's link already being committed by the time our own create attempt lands, while
    our own pre-check ran too early to see it.
    """
    call_count = 0
    real_filter = manager.filter

    def fake_filter(*args: t.Any, **kwargs: t.Any) -> t.Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return manager.none()
        return real_filter(*args, **kwargs)

    with patch.object(manager, "filter", side_effect=fake_filter):
        yield


@pytest.fixture
def connected(organization, organization_owner_user, fake_provider: FakeProvider) -> PlatformConnection:  # type: ignore[no-untyped-def]
    start = connection_service.begin_connect(organization, organization_owner_user, "fake")
    conn = connection_service.complete_connect(start.state, "c")
    conn.auto_sync = True  # prove import does not trigger a push
    conn.save(update_fields=["auto_sync"])
    return conn


@pytest.fixture
def remote(fake_provider: FakeProvider, connected: PlatformConnection) -> str:
    ev = RemoteEvent(
        name="Imported Night",
        summary="Fun.",
        description_html="<p>Come <strong>early</strong>.</p>",
        start=START,
        end=START + timedelta(hours=4),
        timezone="Europe/Vienna",
        currency="EUR",
        venue=RemoteVenue(
            name="Hall", address="Stephansplatz 1", city="Wien", country="AT", latitude=48.2084609, longitude=16.3734547
        ),
    )
    ref = fake_provider.create_event(connected.token(), "acc-1", ev)
    fake_provider.upsert_ticket_class(
        connected.token(),
        ref.remote_id,
        RemoteTicketClass(
            name="Early",
            price=Decimal("12.50"),
            currency="EUR",
            is_free=False,
            quantity_total=40,
            sales_end=START,
            hidden=True,
        ),
    )
    fake_provider.upsert_ticket_class(
        connected.token(),
        ref.remote_id,
        RemoteTicketClass(name="Free", price=Decimal("0"), currency="EUR", is_free=True, quantity_total=10),
    )
    fake_provider.publish_event(connected.token(), ref.remote_id)
    return ref.remote_id


def test_list_remote_events_marks_linked(connected: PlatformConnection, remote: str, organization) -> None:  # type: ignore[no-untyped-def]
    rows = sync_service.list_remote_events(organization, "fake")
    assert [(r.remote_id, r.already_linked, r.status) for r in rows] == [(remote, False, "live")]
    sync_service.import_remote_event(connected, remote)
    rows = sync_service.list_remote_events(organization, "fake")
    assert rows[0].already_linked is True


def test_import_creates_draft_event_tiers_and_link(  # type: ignore[no-untyped-def]
    connected: PlatformConnection, remote: str, django_capture_on_commit_callbacks
) -> None:
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        link = sync_service.import_remote_event(connected, remote)
    assert callbacks == []  # link created last → no auto-sync push scheduled
    event = link.event
    assert (event.status, event.event_type, event.requires_ticket, event.is_virtual) == ("draft", "public", True, False)
    assert event.name == "Imported Night" and event.start == START and event.end == START + timedelta(hours=4)
    assert event.description is not None and "**early**" in event.description
    assert event.address == "Stephansplatz 1"
    tiers = {tier.name: tier for tier in event.ticket_tiers.all()}
    assert set(tiers) == {"Early", "Free"}
    assert (
        tiers["Early"].price == Decimal("12.50")
        and tiers["Early"].total_quantity == 40
        and tiers["Early"].visibility == TicketTier.Visibility.UNLISTED
    )
    assert (
        tiers["Free"].payment_method == TicketTier.PaymentMethod.FREE
        and tiers["Free"].visibility == TicketTier.Visibility.PUBLIC
    )
    assert (link.origin, link.remote_status, link.sync_state, link.remote_id) == ("imported", "live", "in_sync", remote)
    assert link.last_pulled_at is not None and link.remote_url.endswith(remote)
    assert TierLink.objects.filter(event_link=link).count() == 2


def test_import_is_idempotent(connected: PlatformConnection, remote: str) -> None:
    first = sync_service.import_remote_event(connected, remote)
    second = sync_service.import_remote_event(connected, remote)
    assert first.id == second.id and Event.objects.filter(organization=connected.organization).count() == 1


def test_import_resolves_nearest_city(connected: PlatformConnection, remote: str) -> None:
    from django.contrib.gis.geos import Point

    from geo.models import City

    City.objects.create(
        name="Wien",
        ascii_name="Wien",
        country="Austria",
        city_id=99,
        location=Point(16.3734547, 48.2084609, srid=4326),
        timezone="Europe/Vienna",
    )
    link = sync_service.import_remote_event(connected, remote)
    assert link.event.city is not None and link.event.city.name == "Wien"


def test_request_import_queues_and_skips_linked(  # type: ignore[no-untyped-def]
    connected: PlatformConnection, remote: str, organization, django_capture_on_commit_callbacks
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        result = sync_service.request_import(organization, "fake", [remote])
    assert result.queued == [remote] and result.skipped == []
    assert EventLink.objects.filter(connection=connected, remote_id=remote).exists()
    result = sync_service.request_import(organization, "fake", [remote])
    assert result.queued == [] and result.skipped == [remote]


def test_import_skips_invalid_tier_and_reports_it(connected: PlatformConnection, fake_provider: FakeProvider) -> None:
    """A ticket class that fails tier validation is skipped and reported; valid ones still import."""
    ev = RemoteEvent(name="Partly Valid", start=START, end=START + timedelta(hours=2), timezone="UTC", currency="EUR")
    ref = fake_provider.create_event(connected.token(), "acc-1", ev)
    fake_provider.upsert_ticket_class(
        connected.token(),
        ref.remote_id,
        RemoteTicketClass(
            name="Bad Window",
            price=Decimal("10"),
            currency="EUR",
            is_free=False,
            quantity_total=5,
            sales_start=START + timedelta(hours=1),  # after the event start → fails _validate_sales_window
        ),
    )
    fake_provider.upsert_ticket_class(
        connected.token(),
        ref.remote_id,
        RemoteTicketClass(name="Good", price=Decimal("5"), currency="EUR", is_free=False, quantity_total=5),
    )

    link = sync_service.import_remote_event(connected, ref.remote_id)

    tiers = list(link.event.ticket_tiers.all())
    assert [tier.name for tier in tiers] == ["Good"]
    assert TierLink.objects.filter(event_link=link).count() == 1
    assert len(link.sync_report) == 1
    assert link.sync_report[0]["scope"] == "tier"
    assert link.sync_report[0]["tier_name"] == "Bad Window"
    assert link.sync_report[0]["tier_id"] is None


def test_import_race_returns_winner_link_and_rolls_back_loser_draft(connected: PlatformConnection, remote: str) -> None:
    """Two callers import the same remote_id; the DB constraint picks a winner and the loser's draft rolls back."""
    winner_event = Event.objects.create(
        organization=connected.organization, name="Winner Draft", start=START, end=START + timedelta(hours=1)
    )
    winner = EventLink.objects.create(
        event=winner_event,
        connection=connected,
        remote_id=remote,
        remote_status=EventLink.RemoteStatus.LIVE,
        sync_state=EventLink.SyncState.IN_SYNC,
        origin=EventLink.Origin.IMPORTED,
    )

    with _force_first_lookup_miss(EventLink.objects):
        link = sync_service.import_remote_event(connected, remote)

    assert link.id == winner.id
    # Only the winner's event remains; the loser's draft (Event + TicketTiers) was rolled back.
    assert Event.objects.filter(organization=connected.organization).count() == 1


def test_import_truncates_long_address(connected: PlatformConnection, fake_provider: FakeProvider) -> None:
    """A remote address over 255 chars is truncated rather than failing full_clean."""
    ev = RemoteEvent(
        name="Long Address",
        start=START,
        end=START + timedelta(hours=1),
        timezone="UTC",
        currency="EUR",
        venue=RemoteVenue(name="Hall", address="A" * 400, latitude=48.2, longitude=16.3),
    )
    ref = fake_provider.create_event(connected.token(), "acc-1", ev)

    link = sync_service.import_remote_event(connected, ref.remote_id)

    assert link.event.address is not None
    assert len(link.event.address) == 255


def test_import_sanitizes_description_html_before_markdown(
    connected: PlatformConnection, fake_provider: FakeProvider
) -> None:
    """A javascript: href is stripped before the HTML is converted to markdown."""
    ev = RemoteEvent(
        name="XSS Attempt",
        start=START,
        end=START + timedelta(hours=1),
        timezone="UTC",
        currency="EUR",
        description_html='<p>Hi <a href="javascript:alert(1)">x</a></p>',
    )
    ref = fake_provider.create_event(connected.token(), "acc-1", ev)

    link = sync_service.import_remote_event(connected, ref.remote_id)

    assert link.event.description is not None
    assert "javascript:" not in link.event.description
