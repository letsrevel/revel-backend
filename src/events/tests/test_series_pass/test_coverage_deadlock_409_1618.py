"""A deadlock on a coverage lock answers a retryable 409, not a 500 (#1618).

The late pass lock of a tier change is out of the global lock order, so Postgres can stop one of two transactions
with SQLSTATE 40P01. The global ``OperationalError`` handler answers that with a 409 and a stable code.
"""

import typing as t
from unittest.mock import patch

import orjson
import psycopg
import pytest
from django.db import OperationalError
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
    assert TicketTier.objects.get(pk=tier.pk).purchasable_by == TicketTier.PurchasableBy.PUBLIC


def test_another_database_error_stays_a_500(series_pass: SeriesPass, organization_owner_user: RevelUser) -> None:
    _, response = _put_stricter_rule(series_pass, organization_owner_user, _error(psycopg.errors.AdminShutdown()))

    assert response.status_code == 500, response.content
    assert "code" not in response.json()
