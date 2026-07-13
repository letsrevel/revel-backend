"""Tests for the 0095 backfill data migration.

``django-test-migrations`` is not an installed dependency, so we exercise the
migration function directly against Django's live app registry (same pattern
as ``test_reanchor_dst_migration.py``) rather than replaying historical
migration state.
"""

import importlib
import typing as t

import pytest
from django.apps import apps as django_apps

from events.models import Payment, Ticket, TicketTier

pytestmark = pytest.mark.django_db

# The migration module name starts with a digit, so it can't be a normal import.
_migration = importlib.import_module("events.migrations.0095_backfill_payment_reservation_id")
backfill_reservation_id = _migration.backfill_reservation_id


def test_backfill_groups_by_session_id(
    tier_factory: t.Callable[..., TicketTier],
    ticket_factory: t.Callable[..., Ticket],
    payment_factory: t.Callable[..., Payment],
) -> None:
    """Rows sharing a stripe_session_id get one reservation_id; distinct sessions differ."""
    tier = tier_factory()
    session_a_payment_1 = payment_factory(ticket_factory(tier=tier), stripe_session_id="cs_test_a")
    session_a_payment_2 = payment_factory(ticket_factory(tier=tier), stripe_session_id="cs_test_a")
    session_b_payment = payment_factory(ticket_factory(tier=tier), stripe_session_id="cs_test_b")

    backfill_reservation_id(django_apps, None)

    for payment in (session_a_payment_1, session_a_payment_2, session_b_payment):
        payment.refresh_from_db()

    assert session_a_payment_1.reservation_id is not None
    assert session_a_payment_1.reservation_id == session_a_payment_2.reservation_id
    assert session_b_payment.reservation_id is not None
    assert session_b_payment.reservation_id != session_a_payment_1.reservation_id
