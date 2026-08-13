"""Tests for the 0116 backfill data migration (layered ``max_tickets_per_user``).

``django-test-migrations`` is not an installed dependency, so we exercise the migration
function directly against Django's live app registry (same pattern as
``test_backfill_reservation_id.py``). That is sound here: the two fields the migration
touches have the same shape in the historical and current model state.
"""

import importlib
import typing as t

import pytest
from django.apps import apps as django_apps

from events.models import Event, TicketTier

pytestmark = pytest.mark.django_db

# The migration module name starts with a digit, so it can't be a normal import.
_migration = importlib.import_module("events.migrations.0116_backfill_layered_ticket_caps")
backfill_layered_caps = _migration.backfill_layered_caps


def set_event_cap(event: Event, cap: int | None) -> None:
    """Set the event cap without going through ``save()``.

    A ``post_save`` signal re-creates a default ticket tier for any ``requires_ticket``
    event that has none, which would silently repopulate the tier sets these tests
    declare. A queryset update sidesteps it.
    """
    Event.objects.filter(pk=event.pk).update(max_tickets_per_user=cap)
    event.refresh_from_db(fields=["max_tickets_per_user"])


@pytest.fixture
def bare_event(event: Event) -> Event:
    """The ``event`` fixture minus the auto-created default tier, so each test below
    declares its tier set exactly."""
    TicketTier.objects.filter(event=event).delete()
    return event


def test_materializes_inherited_cap_and_clears_event_cap(
    bare_event: Event,
    tier_factory: t.Callable[..., TicketTier],
) -> None:
    """Event cap 1 + tier A cap 4 + tier B cap NULL → tier A 4, tier B 1, event NULL.

    Old semantics gave tier A an allowance of 4 (its override replaced the event value)
    and tier B an allowance of 1 (inherited). The backfill must reproduce exactly that.
    """
    set_event_cap(bare_event, 1)
    tier_a = tier_factory(event=bare_event, name="A", max_tickets_per_user=4)
    tier_b = tier_factory(event=bare_event, name="B", max_tickets_per_user=None)

    backfill_layered_caps(django_apps, None)

    bare_event.refresh_from_db()
    tier_a.refresh_from_db()
    tier_b.refresh_from_db()
    assert tier_a.max_tickets_per_user == 4  # explicit override untouched
    assert tier_b.max_tickets_per_user == 1  # inheritance materialized
    assert bare_event.max_tickets_per_user is None  # no cross-tier total on top


def test_event_with_null_cap_is_left_alone(
    bare_event: Event,
    tier_factory: t.Callable[..., TicketTier],
) -> None:
    """An organizer's explicit "unlimited" (NULL) event cap keeps its tiers unlimited too."""
    set_event_cap(bare_event, None)
    tier_null = tier_factory(event=bare_event, name="A", max_tickets_per_user=None)
    tier_capped = tier_factory(event=bare_event, name="B", max_tickets_per_user=3)

    backfill_layered_caps(django_apps, None)

    bare_event.refresh_from_db()
    tier_null.refresh_from_db()
    tier_capped.refresh_from_db()
    assert bare_event.max_tickets_per_user is None
    assert tier_null.max_tickets_per_user is None
    assert tier_capped.max_tickets_per_user == 3


def test_event_without_tiers_keeps_its_cap(bare_event: Event) -> None:
    """Nothing to preserve on a tier-less event, so its cap survives as the cross-tier total."""
    set_event_cap(bare_event, 2)
    assert not TicketTier.objects.filter(event=bare_event).exists()

    backfill_layered_caps(django_apps, None)

    bare_event.refresh_from_db()
    assert bare_event.max_tickets_per_user == 2


def test_backfill_is_idempotent(
    bare_event: Event,
    tier_factory: t.Callable[..., TicketTier],
) -> None:
    """A second run is a no-op: the first run leaves nothing left to match."""
    set_event_cap(bare_event, 3)
    tier_a = tier_factory(event=bare_event, name="A", max_tickets_per_user=7)
    tier_b = tier_factory(event=bare_event, name="B", max_tickets_per_user=None)

    backfill_layered_caps(django_apps, None)
    backfill_layered_caps(django_apps, None)

    bare_event.refresh_from_db()
    tier_a.refresh_from_db()
    tier_b.refresh_from_db()
    assert bare_event.max_tickets_per_user is None
    assert tier_a.max_tickets_per_user == 7
    assert tier_b.max_tickets_per_user == 3
