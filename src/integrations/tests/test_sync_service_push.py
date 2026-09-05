"""Push orchestration against the FakeProvider: create, update, tier reconciliation, status rule, failures."""

from decimal import Decimal

import pytest

from events.models import Event, TicketTier
from integrations.exceptions import IntegrationError, ProviderError, RetryableProviderError
from integrations.models import EventLink, PlatformConnection, TierLink
from integrations.providers.base import RemoteTicketClass
from integrations.schema import IntegrationErrorCode
from integrations.service import connection_service, sync_service
from integrations.tests.fake_provider import FakeProvider

pytestmark = pytest.mark.django_db


@pytest.fixture
def connected(organization, organization_owner_user, fake_provider: FakeProvider) -> PlatformConnection:  # type: ignore[no-untyped-def]
    start = connection_service.begin_connect(organization, organization_owner_user, "fake")
    return connection_service.complete_connect(start.state, "c")


@pytest.fixture
def clean_event(event: Event) -> Event:
    event.ticket_tiers.all().delete()
    TicketTier.objects.create(
        event=event, name="GA", price=Decimal("10"), total_quantity=100, payment_method=TicketTier.PaymentMethod.ONLINE
    )
    return event


def test_request_push_creates_pending_link_and_dispatches(  # type: ignore[no-untyped-def]
    clean_event: Event, connected: PlatformConnection, django_capture_on_commit_callbacks
) -> None:
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        link = sync_service.request_push(clean_event, "fake")
    assert link.sync_state == EventLink.SyncState.PENDING
    assert len(callbacks) == 1  # the task dispatch


def test_request_push_rejects_ineligible(clean_event: Event, connected: PlatformConnection) -> None:
    clean_event.event_type = Event.EventType.PRIVATE
    clean_event.save()
    with pytest.raises(IntegrationError) as exc:
        sync_service.request_push(clean_event, "fake")
    assert exc.value.code == IntegrationErrorCode.EVENT_PRIVATE and exc.value.status == 400


def test_request_push_requires_active_connection(clean_event: Event, fake_provider: FakeProvider) -> None:
    with pytest.raises(IntegrationError) as exc:
        sync_service.request_push(clean_event, "fake")
    assert exc.value.code == IntegrationErrorCode.PROVIDER_NOT_CONNECTED


def test_first_push_creates_draft_with_tiers_and_report(
    clean_event: Event, connected: PlatformConnection, fake_provider: FakeProvider
) -> None:
    link = sync_service.ensure_link(clean_event, connected)
    link = sync_service.push_link(link)
    assert (link.remote_id, link.remote_status, link.sync_state) == ("ev-1", "draft", "in_sync")
    assert link.remote_url.endswith("/ev-1") and link.last_pushed_at is not None
    remote = fake_provider.get_event(connected.token(), "ev-1")
    assert [c.name for c in remote.ticket_classes] == ["GA"]
    tl = TierLink.objects.get(event_link=link)
    assert tl.remote_id == "tc-1" and tl.tier is not None and tl.tier.name == "GA"
    assert [e["code"] for e in link.sync_report] == ["image_missing"]
    assert "publish_event" not in [c[0] for c in fake_provider.calls]


def test_second_push_updates_in_place_and_reconciles_tiers(
    clean_event: Event, connected: PlatformConnection, fake_provider: FakeProvider
) -> None:
    link = sync_service.push_link(sync_service.ensure_link(clean_event, connected))
    ga = clean_event.ticket_tiers.get(name="GA")
    ga.name = "General"
    ga.save()
    TicketTier.objects.create(
        event=clean_event,
        name="VIP",
        price=Decimal("50"),
        total_quantity=10,
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )
    link = sync_service.push_link(link)
    remote = fake_provider.get_event(connected.token(), "ev-1")
    assert sorted(c.name for c in remote.ticket_classes) == ["General", "VIP"]
    assert TierLink.objects.filter(event_link=link).count() == 2
    assert fake_provider.calls.count(("create_event", "acc-1")) == 1


def test_removed_tier_deleted_when_unsold_hidden_when_sold(
    clean_event: Event, connected: PlatformConnection, fake_provider: FakeProvider
) -> None:
    vip = TicketTier.objects.create(
        event=clean_event,
        name="VIP",
        price=Decimal("50"),
        total_quantity=10,
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )
    link = sync_service.push_link(sync_service.ensure_link(clean_event, connected))
    vip_remote_id = TierLink.objects.get(event_link=link, tier=vip).remote_id
    ga = clean_event.ticket_tiers.get(name="GA")
    ga_remote_id = TierLink.objects.get(event_link=link, tier=ga).remote_id
    # The platform sold 3 VIP tickets meanwhile; Revel knows nothing about it yet (counts arrive in phase 3).
    for c in fake_provider.events["ev-1"].ticket_classes:
        if c.remote_id == vip_remote_id:
            c.quantity_sold = 3
    vip.delete()  # the TierLink survives with tier=None, carrying the remote id
    ga.delete()
    TicketTier.objects.create(
        event=clean_event,
        name="Late",
        price=Decimal("20"),
        total_quantity=10,
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )
    link = sync_service.push_link(link)
    names = {c.name: c for c in fake_provider.get_event(connected.token(), "ev-1").ticket_classes}
    assert set(names) == {"VIP", "Late"} and names["VIP"].hidden is True
    assert ("delete_ticket_class", "ev-1", ga_remote_id) in fake_provider.calls
    assert ("set_ticket_class_paused", "ev-1", vip_remote_id) in fake_provider.calls
    # GA's link went with its class; VIP's is kept (tier=None) so the hidden class stays accounted for.
    assert {(tl.tier_id is None, tl.remote_id) for tl in TierLink.objects.filter(event_link=link)} == {
        (True, vip_remote_id),
        (False, TierLink.objects.get(event_link=link, tier__name="Late").remote_id),
    }
    assert [tl.tier_name for tl in sync_service.to_link_schema(link).tiers] == ["Late"]  # orphans stay out


def test_remote_only_class_is_left_alone_and_reported(
    clean_event: Event, connected: PlatformConnection, fake_provider: FakeProvider
) -> None:
    link = sync_service.push_link(sync_service.ensure_link(clean_event, connected))
    # The organizer adds a ticket class directly on the platform; Revel has never seen it.
    fake_provider.upsert_ticket_class(
        connected.token(),
        "ev-1",
        RemoteTicketClass(name="Door", price=Decimal("5"), currency="EUR", is_free=False, quantity_total=5),
    )
    link = sync_service.push_link(link)
    remote = fake_provider.get_event(connected.token(), "ev-1")
    assert sorted(c.name for c in remote.ticket_classes) == ["Door", "GA"]
    assert not [c for c in fake_provider.calls if c[0] in ("delete_ticket_class", "set_ticket_class_paused")]
    entry = next(e for e in link.sync_report if e["code"] == IntegrationErrorCode.REMOTE_ONLY_TIER.value)
    assert entry["scope"] == "tier" and entry["tier_id"] is None and entry["tier_name"] == "Door"


def test_unmappable_tier_is_hidden_then_unhidden_without_touching_remote_paused(
    clean_event: Event, connected: PlatformConnection, fake_provider: FakeProvider
) -> None:
    link = sync_service.push_link(sync_service.ensure_link(clean_event, connected))
    ga = clean_event.ticket_tiers.get(name="GA")
    remote_id = TierLink.objects.get(event_link=link, tier=ga).remote_id
    for c in fake_provider.events["ev-1"].ticket_classes:  # sold, so it is hidden rather than deleted
        c.quantity_sold = 2
    ga.price_type = TicketTier.PriceType.PWYC
    ga.pwyc_min = Decimal("1")
    ga.save()

    link = sync_service.push_link(link)

    assert fake_provider.get_event(connected.token(), "ev-1").ticket_classes[0].hidden is True
    tl = TierLink.objects.get(event_link=link, tier=ga)
    assert tl.remote_paused is False  # that flag means "the organizer pressed pause", not this
    pauses = fake_provider.calls.count(("set_ticket_class_paused", "ev-1", remote_id))
    sync_service.push_link(link)
    assert fake_provider.calls.count(("set_ticket_class_paused", "ev-1", remote_id)) == pauses  # already hidden

    ga.price_type = TicketTier.PriceType.FIXED
    ga.save()
    sync_service.push_link(link)
    assert fake_provider.get_event(connected.token(), "ev-1").ticket_classes[0].hidden is False


def test_class_deleted_on_the_platform_is_recreated(
    clean_event: Event, connected: PlatformConnection, fake_provider: FakeProvider
) -> None:
    link = sync_service.push_link(sync_service.ensure_link(clean_event, connected))
    tl = TierLink.objects.get(event_link=link)
    fake_provider.delete_ticket_class(connected.token(), "ev-1", tl.remote_id)  # gone on the platform

    link = sync_service.push_link(link)

    assert link.sync_state == EventLink.SyncState.IN_SYNC
    remote = fake_provider.get_event(connected.token(), "ev-1")
    assert [c.name for c in remote.ticket_classes] == ["GA"]
    tl.refresh_from_db()
    assert tl.remote_id == remote.ticket_classes[0].remote_id


def test_ineligible_event_fails_the_push_without_calling_the_platform(
    clean_event: Event, connected: PlatformConnection, fake_provider: FakeProvider
) -> None:
    clean_event.event_type = Event.EventType.PRIVATE
    clean_event.save()
    link = sync_service.push_link(sync_service.ensure_link(clean_event, connected))
    assert link.sync_state == EventLink.SyncState.FAILED
    assert [e["code"] for e in link.sync_report] == [IntegrationErrorCode.EVENT_PRIVATE.value]
    assert fake_provider.calls == []


def test_cancelled_listing_short_circuits(
    clean_event: Event, connected: PlatformConnection, fake_provider: FakeProvider
) -> None:
    link = sync_service.push_link(sync_service.ensure_link(clean_event, connected))
    link.remote_status = EventLink.RemoteStatus.CANCELLED
    link.save(update_fields=["remote_status"])
    fake_provider.calls.clear()
    link = sync_service.push_link(link)
    assert link.sync_state == EventLink.SyncState.IN_SYNC and fake_provider.calls == []


def test_cleared_description_is_pushed_as_empty(
    clean_event: Event, connected: PlatformConnection, fake_provider: FakeProvider
) -> None:
    clean_event.description = None
    clean_event.save()
    link = sync_service.push_link(sync_service.ensure_link(clean_event, connected))
    assert ("set_description", link.remote_id) in fake_provider.calls
    assert fake_provider.get_event(connected.token(), link.remote_id).description_html == ""


def test_live_link_mirrors_cancelled_and_warns_on_draft(
    clean_event: Event, connected: PlatformConnection, fake_provider: FakeProvider
) -> None:
    link = sync_service.push_link(sync_service.ensure_link(clean_event, connected))
    link.remote_status = EventLink.RemoteStatus.LIVE
    link.save(update_fields=["remote_status"])
    fake_provider.publish_event(connected.token(), "ev-1")
    clean_event.status = Event.EventStatus.DRAFT
    clean_event.save()
    link = sync_service.push_link(link)
    assert IntegrationErrorCode.UNPUBLISH_REFUSED.value in [e["code"] for e in link.sync_report]
    assert fake_provider.get_event(connected.token(), "ev-1").status == "live"
    clean_event.status = Event.EventStatus.CANCELLED
    clean_event.save()
    link = sync_service.push_link(link)
    assert link.remote_status == EventLink.RemoteStatus.CANCELLED
    assert fake_provider.get_event(connected.token(), "ev-1").status == "cancelled"


def test_remote_missing_marks_broken_and_repush_recreates(
    clean_event: Event, connected: PlatformConnection, fake_provider: FakeProvider
) -> None:
    link = sync_service.push_link(sync_service.ensure_link(clean_event, connected))
    fake_provider.missing.add("ev-1")
    link = sync_service.push_link(link)
    assert link.sync_state == EventLink.SyncState.BROKEN
    assert [e["code"] for e in link.sync_report][-1] == "remote_event_missing"
    fake_provider.missing.clear()
    link = sync_service.push_link(link)
    assert link.remote_id == "ev-2" and link.sync_state == EventLink.SyncState.IN_SYNC


def test_provider_rejected_marks_failed_with_message(
    clean_event: Event, connected: PlatformConnection, fake_provider: FakeProvider
) -> None:
    fake_provider.fail["create_event"] = ProviderError(IntegrationErrorCode.PROVIDER_REJECTED, "SUMMARY_TOO_LONG")
    link = sync_service.push_link(sync_service.ensure_link(clean_event, connected))
    assert link.sync_state == EventLink.SyncState.FAILED
    assert link.sync_report[-1] == {
        "scope": "event",
        "tier_id": None,
        "tier_name": None,
        "code": "provider_rejected",
        "detail": link.sync_report[-1]["detail"],
        "provider_message": "SUMMARY_TOO_LONG",
    }


def test_retryable_error_reraises_for_celery(
    clean_event: Event, connected: PlatformConnection, fake_provider: FakeProvider
) -> None:
    fake_provider.fail["create_event"] = ProviderError(
        IntegrationErrorCode.PROVIDER_RATE_LIMITED, "429", retryable=True
    )
    link = sync_service.ensure_link(clean_event, connected)
    with pytest.raises(RetryableProviderError):
        sync_service.push_link(link)
    link.refresh_from_db()
    assert link.sync_state == EventLink.SyncState.PENDING


def test_revoked_connection_marks_connection_and_fails_link(
    clean_event: Event, connected: PlatformConnection, fake_provider: FakeProvider
) -> None:
    fake_provider.fail["create_event"] = ProviderError(IntegrationErrorCode.CONNECTION_REVOKED, "401")
    link = sync_service.push_link(sync_service.ensure_link(clean_event, connected))
    connected.refresh_from_db()
    assert connected.status == PlatformConnection.Status.ERROR
    assert link.sync_state == EventLink.SyncState.FAILED
