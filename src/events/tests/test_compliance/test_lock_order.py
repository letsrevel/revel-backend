"""Canonical lock order on the series-pass activation paths (docs/engineering-notes.md).

parent row -> TicketTier (pk) -> Ticket -> TicketNumberSequence. Both activation paths
backfill (locking tiers) and number tickets (locking the per-org sequence); the tier lock
must come first, or two transactions can wait on each other in a cycle.
"""

import typing as t
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from accounts.models import RevelUser
from events.models import EventSeries, HeldSeriesPass, Organization, TicketTier
from events.service import series_pass_service
from events.service.series_pass_purchase import SeriesPassPurchaseService
from events.service.stripe_webhooks import StripeEventHandler
from events.tasks.series_pass import materialize_series_pass_holders
from events.tests.test_series_pass.test_backfill import (
    _completed_checkout_event,
    _make_covered_event,
    _make_pass,
    _purchase_online,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def stripe_connected_organization(organization: Organization) -> Organization:
    organization.stripe_account_id = "acct_lock_order"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.save()
    return organization


def _first_lock(queries: list[dict[str, t.Any]], table: str) -> int:
    """Index of the first ``SELECT ... FOR UPDATE`` on ``table`` (fails if there is none)."""
    return next(i for i, q in enumerate(queries) if f'FROM "{table}"' in q["sql"] and "FOR UPDATE" in q["sql"])


def _assert_tiers_locked_before_sequence(queries: list[dict[str, t.Any]]) -> None:
    assert _first_lock(queries, "events_tickettier") < _first_lock(queries, "events_ticketnumbersequence")


def _extend_while_pending(org: Organization, event_series: EventSeries, series_pass: t.Any, slug: str) -> None:
    """Link a new event while the pass is PENDING, so activation has to backfill (lock tiers)."""
    new_event, _, _ = _make_covered_event(org, event_series, series_pass, slug)
    with patch("notifications.signals.series_pass.send_series_pass_extended"):
        materialize_series_pass_holders(str(series_pass.id), [str(new_event.id)])


def test_offline_confirm_locks_tiers_before_numbering(
    organization: Organization, event_series: EventSeries, member_user: RevelUser
) -> None:
    series_pass = _make_pass(organization, event_series, TicketTier.PaymentMethod.OFFLINE, "lo-off")
    held_pass = SeriesPassPurchaseService(series_pass, member_user).purchase()
    assert isinstance(held_pass, HeldSeriesPass)
    _extend_while_pending(organization, event_series, series_pass, "lo-off-new")

    with (
        patch("events.service.series_pass_service.send_series_pass_purchased"),
        CaptureQueriesContext(connection) as ctx,
    ):
        series_pass_service.confirm_held_pass_payment(held_pass)

    _assert_tiers_locked_before_sequence(ctx.captured_queries)


def test_webhook_activation_locks_tiers_before_numbering(
    stripe_connected_organization: Organization, event_series: EventSeries, member_user: RevelUser
) -> None:
    series_pass = _make_pass(stripe_connected_organization, event_series, TicketTier.PaymentMethod.ONLINE, "lo-on")
    _purchase_online(series_pass, member_user, "cs_lock_order")
    _extend_while_pending(stripe_connected_organization, event_series, series_pass, "lo-on-new")

    event = _completed_checkout_event("cs_lock_order")
    with CaptureQueriesContext(connection) as ctx:
        StripeEventHandler(event).handle_checkout_session_completed(event)

    _assert_tiers_locked_before_sequence(ctx.captured_queries)
