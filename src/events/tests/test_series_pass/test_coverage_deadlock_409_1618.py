"""A deadlock on a coverage lock answers a retryable 409, not a 500 (#1618).

The late pass lock of a tier change is out of the global lock order, so Postgres can stop one of two transactions
with SQLSTATE 40P01. The global ``OperationalError`` handler answers that with a 409 and a stable code.
"""

import typing as t
from unittest.mock import Mock, patch

import orjson
import psycopg
import pytest
from django.db import OperationalError, transaction
from django.test.client import Client
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import SeriesPass, TicketTier
from events.tests.test_series_pass.covered_events import cover

pytestmark = pytest.mark.django_db


def _error(cause: Exception) -> OperationalError:
    error = OperationalError(str(cause))
    error.__cause__ = cause
    return error


def _put_stricter_rule(series_pass: SeriesPass, user: RevelUser, error: OperationalError) -> tuple[TicketTier, t.Any]:
    _, tier = cover(series_pass, "deadlock", days=3)
    client = Client(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")  # type: ignore[attr-defined]
    url = reverse("api:update_ticket_tier", kwargs={"event_id": tier.event_id, "tier_id": tier.id})
    with patch("events.models.ticket.lock_rows", side_effect=error):  # the pass lock of the tier check
        response = client.put(
            url,
            data=orjson.dumps({"purchasable_by": TicketTier.PurchasableBy.MEMBERS}),
            content_type="application/json",
        )
    return tier, response


def test_a_deadlock_answers_a_retryable_409(series_pass: SeriesPass, organization_owner_user: RevelUser) -> None:
    tier, response = _put_stricter_rule(series_pass, organization_owner_user, _error(psycopg.errors.DeadlockDetected()))

    assert response.status_code == 409, response.content
    assert response.json()["code"] == "concurrent_change"
    assert "try again" in response.json()["detail"]


def test_another_database_error_stays_a_500(series_pass: SeriesPass, organization_owner_user: RevelUser) -> None:
    _, response = _put_stricter_rule(series_pass, organization_owner_user, _error(psycopg.errors.AdminShutdown()))

    assert response.status_code == 500, response.content
    assert "code" not in response.json()


@pytest.mark.django_db(transaction=True)
def test_a_deadlock_saves_nothing_of_the_request(series_pass: SeriesPass, organization_owner_user: RevelUser) -> None:
    """A write outside any inner savepoint and an ``on_commit`` callback are dropped with the 409.

    The service stand-in writes and queues a callback in the request transaction itself, then fails with a deadlock.
    Without the handler's rollback mark, ``ATOMIC_REQUESTS`` would commit both. ``transaction=True``: the request
    must commit or roll back for real, which the rollback-wrapped default ``django_db`` transaction hides.
    """
    _, tier = cover(series_pass, "deadlock-rollback", days=3)
    client = Client(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(organization_owner_user).access_token}")  # type: ignore[attr-defined]
    url = reverse("api:update_ticket_tier", kwargs={"event_id": tier.event_id, "tier_id": tier.id})
    after_commit = Mock()

    def write_then_deadlock(target: TicketTier, payload: t.Any) -> TicketTier:
        TicketTier.objects.filter(pk=target.pk).update(name="Written before the deadlock")
        transaction.on_commit(after_commit)
        raise _error(psycopg.errors.DeadlockDetected())

    with patch("events.service.ticket_service.update_ticket_tier", side_effect=write_then_deadlock):
        response = client.put(url, data=orjson.dumps({"name": "x"}), content_type="application/json")

    assert response.status_code == 409, response.content
    assert TicketTier.objects.get(pk=tier.pk).name == tier.name
    after_commit.assert_not_called()
