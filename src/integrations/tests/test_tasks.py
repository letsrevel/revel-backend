"""Celery task wrappers: names pinned, retries bounded, and the link row never lies."""

import uuid
from decimal import Decimal

import pytest

from events.models import Event, TicketTier
from integrations import registry, tasks
from integrations.enums import IntegrationErrorCode
from integrations.exceptions import ProviderError, RetryableProviderError
from integrations.models import EventLink, PlatformConnection
from integrations.service import connection_service, sync_service
from integrations.tests.fake_provider import FakeProvider


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


def test_task_names_are_pinned() -> None:
    assert tasks.push_event_link.name == "integrations.push_event_link"
    assert tasks.import_remote_event.name == "integrations.import_remote_event"


def test_push_task_retry_budget_is_bounded() -> None:
    assert tasks.push_event_link.max_retries == tasks.MAX_RETRIES == 5
    assert tasks._retry_countdown(0) == 30 and tasks._retry_countdown(9) == 600


def test_push_task_ignores_missing_link(db: None) -> None:
    tasks.push_event_link(str(uuid.uuid4()))  # no raise: link deleted between dispatch and run


@pytest.mark.django_db
def test_push_task_skips_inactive_connection(pushed: EventLink, connected: PlatformConnection) -> None:
    connected.status = PlatformConnection.Status.REVOKED
    connected.save(update_fields=["status"])
    tasks.push_event_link(str(pushed.id))
    pushed.refresh_from_db()
    assert pushed.sync_state == EventLink.SyncState.IN_SYNC  # untouched


@pytest.mark.django_db
def test_push_task_skips_disabled_provider(pushed: EventLink, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "PROVIDERS", {})
    tasks.push_event_link(str(pushed.id))  # no raise: the provider is gone, not the link


@pytest.mark.django_db
def test_push_task_keeps_pending_and_reports_a_rate_limit(pushed: EventLink, fake_provider: FakeProvider) -> None:
    fake_provider.fail["update_event"] = ProviderError(
        IntegrationErrorCode.PROVIDER_RATE_LIMITED, "429", retryable=True
    )
    with pytest.raises(RetryableProviderError):
        tasks.push_event_link(str(pushed.id))
    pushed.refresh_from_db()
    assert pushed.sync_state == EventLink.SyncState.PENDING
    assert [e["code"] for e in pushed.sync_report] == [IntegrationErrorCode.PROVIDER_RATE_LIMITED.value]


@pytest.mark.django_db
def test_push_task_fails_when_the_retry_budget_is_spent(pushed: EventLink, fake_provider: FakeProvider) -> None:
    fake_provider.fail["update_event"] = ProviderError(
        IntegrationErrorCode.PROVIDER_RATE_LIMITED, "429", retryable=True
    )
    # `apply(retries=...)` runs the task eagerly with a spent budget; celery then re-raises the
    # original exception instead of MaxRetriesExceededError because we hand it an ``exc``.
    with pytest.raises(RetryableProviderError):
        tasks.push_event_link.apply(args=(str(pushed.id),), retries=tasks.MAX_RETRIES)
    pushed.refresh_from_db()
    assert pushed.sync_state == EventLink.SyncState.FAILED
    assert [e["code"] for e in pushed.sync_report] == [IntegrationErrorCode.PROVIDER_RATE_LIMITED.value]


@pytest.mark.django_db
def test_push_task_marks_failed_on_an_unexpected_exception(pushed: EventLink, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(link: EventLink) -> EventLink:
        raise RuntimeError("boom")

    monkeypatch.setattr(sync_service, "push_link", _boom)
    with pytest.raises(RuntimeError):
        tasks.push_event_link(str(pushed.id))
    pushed.refresh_from_db()
    assert pushed.sync_state == EventLink.SyncState.FAILED
    assert pushed.sync_report[0]["code"] == IntegrationErrorCode.PROVIDER_REJECTED.value
    assert pushed.sync_report[0]["provider_message"] == "boom"


@pytest.mark.django_db
def test_import_task_skips_inactive_connection(connected: PlatformConnection) -> None:
    connected.status = PlatformConnection.Status.REVOKED
    connected.save(update_fields=["status"])
    tasks.import_remote_event(str(connected.id), "ev-1")  # no raise, nothing imported
    assert not EventLink.objects.filter(connection=connected).exists()


@pytest.mark.django_db
def test_import_task_skips_disabled_provider(connected: PlatformConnection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "PROVIDERS", {})
    tasks.import_remote_event(str(connected.id), "ev-1")  # no raise, nothing imported
    assert not EventLink.objects.filter(connection=connected).exists()


def test_beat_task_names_are_pinned() -> None:
    assert tasks.reconcile_counts.name == "integrations.reconcile_counts"
    assert tasks.prune_webhook_deliveries.name == "integrations.prune_webhook_deliveries"
    assert tasks.handle_webhook_delivery.name == "integrations.handle_webhook_delivery"
    assert tasks.refresh_link_counts.name == "integrations.refresh_link_counts"


def test_refresh_counts_task_ignores_missing_link(db: None) -> None:
    tasks.refresh_link_counts(str(uuid.uuid4()))  # no raise: link deleted before the delayed run


@pytest.mark.django_db
def test_refresh_counts_task_skips_inactive_connection(pushed: EventLink, connected: PlatformConnection) -> None:
    connected.status = PlatformConnection.Status.REVOKED
    connected.save(update_fields=["status"])
    tasks.refresh_link_counts(str(pushed.id))
    assert not pushed.tier_links.filter(counts_updated_at__isnull=False).exists()


@pytest.mark.django_db
def test_refresh_counts_task_skips_disabled_provider(pushed: EventLink, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "PROVIDERS", {})
    tasks.refresh_link_counts(str(pushed.id))  # no raise: the provider is gone, not the link
    assert not pushed.tier_links.filter(counts_updated_at__isnull=False).exists()


def test_beat_rows_exist(db: None) -> None:
    from django_celery_beat.models import PeriodicTask

    rows = {p.name: p for p in PeriodicTask.objects.filter(task__startswith="integrations.")}
    assert rows["Reconcile platform listing counts"].interval.every == 15
    assert rows["Reconcile platform listing counts"].interval.period == "minutes"
    assert rows["Prune platform webhook deliveries"].crontab.hour == "4"


@pytest.mark.django_db
def test_transient_failure_keeps_the_providers_own_error_code(pushed: EventLink, fake_provider: FakeProvider) -> None:
    """5xx and transport errors are retryable too, but they are not rate limits.

    The report codes are a stable contract the frontend renders copy from, so recording every
    transient failure as ``provider_rate_limited`` tells the organizer the wrong story.
    """
    fake_provider.fail["update_event"] = ProviderError(
        IntegrationErrorCode.PROVIDER_REJECTED, "502 Bad Gateway", retryable=True
    )
    with pytest.raises(RetryableProviderError):
        tasks.push_event_link(str(pushed.id))
    pushed.refresh_from_db()
    assert [e["code"] for e in pushed.sync_report] == [IntegrationErrorCode.PROVIDER_REJECTED.value]
