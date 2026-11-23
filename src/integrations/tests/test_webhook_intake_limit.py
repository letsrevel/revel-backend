"""Per-connection intake limit and one budget-deferred delivery per connection (#1466)."""

import typing as t
from decimal import Decimal
from unittest import mock

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse
from structlog.testing import capture_logs

from accounts.models import RevelUser
from events.models import Event, Organization, TicketTier
from integrations import tasks
from integrations.models import EventLink, PlatformConnection, WebhookDelivery
from integrations.service import connection_service, sync_service
from integrations.tests.fake_provider import FakeProvider

pytestmark = pytest.mark.django_db


def _connect(organization: Organization, owner: RevelUser) -> PlatformConnection:
    start = connection_service.begin_connect(organization, owner, "fake")
    return connection_service.complete_connect(start.state, "c")


@pytest.fixture
def connected(
    organization: Organization, organization_owner_user: RevelUser, fake_provider: FakeProvider
) -> PlatformConnection:
    return _connect(organization, organization_owner_user)


@pytest.fixture
def pushed(event: Event, connected: PlatformConnection) -> EventLink:
    event.ticket_tiers.all().delete()
    TicketTier.objects.create(
        event=event, name="GA", price=Decimal("10"), total_quantity=100, payment_method=TicketTier.PaymentMethod.ONLINE
    )
    return sync_service.push_link(sync_service.ensure_link(event, connected))


def _post(conn: PlatformConnection, path: str) -> int:
    url = reverse("api:integration_webhook", kwargs={"provider": "fake", "secret": conn.webhook_secret})
    response = Client().post(
        url, data=orjson.dumps({"action": "order.placed", "path": path}), content_type="application/json"
    )
    return response.status_code


def _deliver(conn: PlatformConnection, path: str = "/orders/ord-1/") -> WebhookDelivery:
    return WebhookDelivery.objects.create(
        connection=conn, action="order.placed", resource_path=path, payload={"action": "order.placed", "path": path}
    )


def _low_budget(fake_provider: FakeProvider, settings: t.Any) -> None:
    settings.INTEGRATIONS_RATE_RESERVE = 200
    fake_provider.budget = 150


def test_intake_above_the_limit_answers_200_without_a_row_or_a_task(
    connected: PlatformConnection,
    pushed: EventLink,
    settings: t.Any,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    settings.INTEGRATIONS_WEBHOOK_INTAKE_LIMIT_PER_HOUR = 2
    path = f"/events/{pushed.remote_id}/"
    with (
        mock.patch.object(tasks.handle_webhook_delivery, "delay") as delay,
        capture_logs() as logs,
        django_capture_on_commit_callbacks(execute=True),
    ):
        statuses = [_post(connected, path) for _ in range(3)]
    assert statuses == [200, 200, 200]
    assert WebhookDelivery.objects.filter(connection=connected).count() == 2
    assert delay.call_count == 2
    limited = [e for e in logs if e["event"] == "integration_webhook_intake_limit_reached"]
    assert len(limited) == 1
    assert limited[0]["connection_id"] == str(connected.id)
    assert limited[0]["organization_id"] == str(connected.organization_id)
    assert limited[0]["action"] == "order.placed"
    assert limited[0]["log_level"] == "warning"


def test_intake_limit_is_per_connection(
    connected: PlatformConnection,
    django_user_model: type[RevelUser],
    settings: t.Any,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    settings.INTEGRATIONS_WEBHOOK_INTAKE_LIMIT_PER_HOUR = 1
    other_owner = django_user_model.objects.create_user(username="other_owner", email="o@example.com", password="p")
    other = _connect(Organization.objects.create(name="Other", slug="other", owner=other_owner), other_owner)
    with mock.patch.object(tasks.handle_webhook_delivery, "delay"), django_capture_on_commit_callbacks(execute=True):
        assert _post(connected, "/orders/1/") == 200
        assert _post(connected, "/orders/2/") == 200
        assert _post(other, "/orders/3/") == 200
    assert WebhookDelivery.objects.filter(connection=connected).count() == 1
    assert WebhookDelivery.objects.filter(connection=other).count() == 1


def test_low_budget_keeps_one_deferred_task_per_connection(
    connected: PlatformConnection, pushed: EventLink, fake_provider: FakeProvider, settings: t.Any
) -> None:
    """Only the first deferred delivery is re-enqueued; the others are finished at once, with a log line."""
    _low_budget(fake_provider, settings)
    first, second = _deliver(connected), _deliver(connected, "/orders/ord-2/")
    with mock.patch.object(tasks.handle_webhook_delivery, "apply_async") as apply_async, capture_logs() as logs:
        tasks.handle_webhook_delivery.apply(args=(str(first.id),), throw=True)
        tasks.handle_webhook_delivery.apply(args=(str(second.id),), throw=True)
        # The held delivery keeps its slot when it is deferred again.
        tasks.handle_webhook_delivery.apply(args=(str(first.id),), throw=True)
    assert (
        apply_async.call_args_list
        == [mock.call(args=(str(first.id),), countdown=tasks.WEBHOOK_BUDGET_DEFER_COUNTDOWN_SECONDS)] * 2
    )
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.outcome == WebhookDelivery.Outcome.RECEIVED
    assert second.outcome == WebhookDelivery.Outcome.PROCESSED
    coalesced = [e for e in logs if e["event"] == "integration_webhook_coalesced_budget_low"]
    assert [(e["delivery_id"], e["connection_id"], e["held_by"]) for e in coalesced] == [
        (str(second.id), str(connected.id), str(first.id))
    ]


def test_held_slot_is_released_when_the_deferred_delivery_finishes(
    connected: PlatformConnection, pushed: EventLink, fake_provider: FakeProvider, settings: t.Any
) -> None:
    _low_budget(fake_provider, settings)
    first = _deliver(connected, f"/orders/{pushed.remote_id}/")
    with mock.patch.object(tasks.handle_webhook_delivery, "apply_async"):
        tasks.handle_webhook_delivery.apply(args=(str(first.id),), throw=True)
    fake_provider.budget = None
    with mock.patch.object(tasks.refresh_link_counts, "apply_async"):
        tasks.handle_webhook_delivery.apply(args=(str(first.id),), throw=True)
    first.refresh_from_db()
    assert first.outcome != WebhookDelivery.Outcome.RECEIVED
    _low_budget(fake_provider, settings)
    later = _deliver(connected, "/orders/ord-3/")
    with mock.patch.object(tasks.handle_webhook_delivery, "apply_async") as apply_async:
        tasks.handle_webhook_delivery.apply(args=(str(later.id),), throw=True)
    apply_async.assert_called_once_with(args=(str(later.id),), countdown=tasks.WEBHOOK_BUDGET_DEFER_COUNTDOWN_SECONDS)


def test_slot_is_per_connection(
    connected: PlatformConnection,
    fake_provider: FakeProvider,
    django_user_model: type[RevelUser],
    settings: t.Any,
) -> None:
    _low_budget(fake_provider, settings)
    other_owner = django_user_model.objects.create_user(username="other_owner", email="o@example.com", password="p")
    other = _connect(Organization.objects.create(name="Other", slug="other", owner=other_owner), other_owner)
    mine, theirs = _deliver(connected), _deliver(other)
    with mock.patch.object(tasks.handle_webhook_delivery, "apply_async") as apply_async:
        tasks.handle_webhook_delivery.apply(args=(str(mine.id),), throw=True)
        tasks.handle_webhook_delivery.apply(args=(str(theirs.id),), throw=True)
    assert apply_async.call_count == 2


def test_webhook_task_stores_no_result() -> None:
    assert tasks.handle_webhook_delivery.ignore_result is True
