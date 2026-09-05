"""Auto-sync: only opted-in, already-pushed, eligible links are scheduled; bursts collapse to one push."""

from decimal import Decimal

import pytest
from django.core.cache import cache

from events.models import Event, TicketTier
from events.suppression import suppress_event_notifications
from integrations.models import EventLink, PlatformConnection
from integrations.service import connection_service, sync_service
from integrations.tests.fake_provider import FakeProvider

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    cache.clear()


@pytest.fixture
def connected(organization, organization_owner_user, fake_provider: FakeProvider) -> PlatformConnection:  # type: ignore[no-untyped-def]
    start = connection_service.begin_connect(organization, organization_owner_user, "fake")
    return connection_service.complete_connect(start.state, "c")


@pytest.fixture
def pushed(event: Event, connected: PlatformConnection) -> EventLink:
    event.ticket_tiers.all().delete()
    TicketTier.objects.create(
        event=event, name="GA", price=Decimal("10"), total_quantity=100, payment_method=TicketTier.PaymentMethod.ONLINE
    )
    return sync_service.push_link(sync_service.ensure_link(event, connected))


def test_no_schedule_when_auto_sync_off(pushed: EventLink, django_capture_on_commit_callbacks) -> None:  # type: ignore[no-untyped-def]
    # suppress_event_notifications: an unrelated pre-existing signal (notifications.signals.event)
    # also schedules an on_commit callback whenever Event.name changes; isolate the assertion to
    # auto-sync scheduling only.
    with django_capture_on_commit_callbacks(execute=False) as callbacks, suppress_event_notifications():
        pushed.event.name = "Renamed"
        pushed.event.save()
    assert callbacks == []


def test_event_save_schedules_once_per_burst(  # type: ignore[no-untyped-def]
    pushed: EventLink, connected: PlatformConnection, django_capture_on_commit_callbacks, fake_provider: FakeProvider
) -> None:
    connected.auto_sync = True
    connected.save(update_fields=["auto_sync"])
    # suppress_event_notifications: isolate the burst-collapse assertion from the unrelated
    # notifications.signals.event on-commit callback fired by Event.name changes.
    with django_capture_on_commit_callbacks(execute=False) as callbacks, suppress_event_notifications():
        pushed.event.name = "Renamed"
        pushed.event.save()
        pushed.event.name = "Renamed again"
        pushed.event.save()
        TicketTier.objects.create(
            event=pushed.event,
            name="VIP",
            price=Decimal("50"),
            total_quantity=5,
            payment_method=TicketTier.PaymentMethod.ONLINE,
        )
    assert len(callbacks) == 1
    pushed.refresh_from_db()
    assert pushed.sync_state == EventLink.SyncState.PENDING


def test_per_link_override_wins(  # type: ignore[no-untyped-def]
    pushed: EventLink, connected: PlatformConnection, django_capture_on_commit_callbacks
) -> None:
    connected.auto_sync = True
    connected.save(update_fields=["auto_sync"])
    pushed.auto_sync = False
    pushed.save(update_fields=["auto_sync"])
    with django_capture_on_commit_callbacks(execute=False) as callbacks, suppress_event_notifications():
        pushed.event.name = "Renamed"
        pushed.event.save()
    assert callbacks == []


def test_tier_delete_schedules(pushed: EventLink, django_capture_on_commit_callbacks) -> None:  # type: ignore[no-untyped-def]
    pushed.auto_sync = True
    pushed.save(update_fields=["auto_sync"])
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        pushed.event.ticket_tiers.get(name="GA").delete()
    assert len(callbacks) == 1


def test_unpushed_broken_or_ineligible_links_are_skipped(  # type: ignore[no-untyped-def]
    event: Event, connected: PlatformConnection, django_capture_on_commit_callbacks
) -> None:
    link = sync_service.ensure_link(event, connected)  # remote_id == ""
    link.auto_sync = True
    link.save(update_fields=["auto_sync"])
    # suppress_event_notifications: isolate the assertion from the unrelated
    # notifications.signals.event on-commit callback fired by Event.name changes.
    with django_capture_on_commit_callbacks(execute=False) as callbacks, suppress_event_notifications():
        event.name = "x"
        event.save()
    assert callbacks == []
    link.remote_id = "ev-1"
    link.sync_state = EventLink.SyncState.BROKEN
    link.save(update_fields=["remote_id", "sync_state"])
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        event.save()
    assert callbacks == []
    link.sync_state = EventLink.SyncState.IN_SYNC
    link.save(update_fields=["sync_state"])
    event.event_type = Event.EventType.PRIVATE
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        event.save()
    assert callbacks == []


def test_push_writes_do_not_retrigger(pushed: EventLink, django_capture_on_commit_callbacks) -> None:  # type: ignore[no-untyped-def]
    pushed.auto_sync = True
    pushed.save(update_fields=["auto_sync"])
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        sync_service.push_link(pushed)  # writes only integrations models
    assert callbacks == []


def test_scheduled_task_pushes(  # type: ignore[no-untyped-def]
    pushed: EventLink, connected: PlatformConnection, django_capture_on_commit_callbacks, fake_provider: FakeProvider
) -> None:
    pushed.auto_sync = True
    pushed.save(update_fields=["auto_sync"])
    with django_capture_on_commit_callbacks(execute=True):
        pushed.event.name = "Renamed"
        pushed.event.save()
    assert fake_provider.get_event(connected.token(), pushed.remote_id).name == "Renamed"
    pushed.refresh_from_db()
    assert pushed.sync_state == EventLink.SyncState.IN_SYNC
