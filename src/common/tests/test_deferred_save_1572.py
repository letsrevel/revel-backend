"""A full save of a deferred instance writes only the loaded fields (#1572).

Without ``update_fields``, ``full_clean()`` (and the EXIF strip) read every deferred field, one query each, and
Django then wrote every column, also one that another request had changed meanwhile.
"""

import re
import typing as t
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from events.models import Event, Organization, TicketTier

pytestmark = pytest.mark.django_db


def _update_sql(queries: CaptureQueriesContext, table: str) -> list[str]:
    return [q["sql"] for q in queries.captured_queries if q["sql"].startswith(f'UPDATE "{table}"')]


def test_an_image_model_writes_only_the_loaded_fields(organization: Organization) -> None:
    """Organization has the EXIF mixin and TimeStampedModel: both saves see the deferred instance."""
    loaded = Organization.objects.only("id", "name", "slug", "owner_id").get(pk=organization.pk)
    Organization.objects.filter(pk=organization.pk).update(description="changed meanwhile")
    loaded.name = "Renamed"

    with CaptureQueriesContext(connection) as queries:
        loaded.save()

    [update] = _update_sql(queries, "events_organization")
    assert '"description"' not in update
    assert '"name"' in update
    organization.refresh_from_db()
    assert (organization.name, organization.description) == ("Renamed", "changed meanwhile")


def test_a_plain_model_writes_only_the_loaded_fields(organization: Organization) -> None:
    event = Event.objects.create(organization=organization, name="E", slug="e-1572", start=timezone.now())
    tier = TicketTier.objects.create(event=event, name="T", price=Decimal("5.00"), currency="EUR")
    loaded = TicketTier.objects.only("id", "name", "event_id").get(pk=tier.pk)
    TicketTier.objects.filter(pk=tier.pk).update(price=Decimal("9.00"))
    loaded.name = "Renamed"

    with CaptureQueriesContext(connection) as queries:
        loaded.save()

    [update] = _update_sql(queries, "events_tickettier")
    assert not re.search(r'"price"\s*=', update)
    tier.refresh_from_db()
    assert (tier.name, tier.price) == ("Renamed", Decimal("9.00"))


def test_a_full_instance_still_writes_every_field(organization: Organization) -> None:
    loaded = Organization.objects.get(pk=organization.pk)
    loaded.description = "full"

    with CaptureQueriesContext(connection) as queries:
        loaded.save()

    [update] = _update_sql(queries, "events_organization")
    assert '"description"' in update and '"name"' in update


def test_an_explicit_update_fields_is_kept(organization: Organization) -> None:
    loaded: t.Any = Organization.objects.only("id", "name", "slug", "owner_id").get(pk=organization.pk)
    loaded.name = "Only name"

    with CaptureQueriesContext(connection) as queries:
        loaded.save(update_fields=["name"])

    [update] = _update_sql(queries, "events_organization")
    assert '"slug"' not in update


def test_updated_at_is_written_only_when_it_was_loaded(organization: Organization) -> None:
    """Django's own rule for a deferred save: ``auto_now`` runs only for the fields that are written."""
    stamp = Organization.objects.values_list("updated_at", flat=True).get(pk=organization.pk)
    without = Organization.objects.only("id", "name", "slug", "owner_id").get(pk=organization.pk)
    without.name = "A"
    without.save()
    assert Organization.objects.values_list("updated_at", flat=True).get(pk=organization.pk) == stamp

    with_stamp = Organization.objects.only("id", "name", "slug", "owner_id", "updated_at").get(pk=organization.pk)
    with_stamp.name = "B"
    with_stamp.save()
    assert Organization.objects.values_list("updated_at", flat=True).get(pk=organization.pk) > stamp


def test_post_save_receives_the_loaded_fields(organization: Organization) -> None:
    from django.db.models.signals import post_save

    seen: list[t.Any] = []

    def receiver(sender: t.Any, update_fields: t.Any = None, **kwargs: t.Any) -> None:
        seen.append(update_fields)

    post_save.connect(receiver, sender=Organization, weak=False)
    try:
        loaded = Organization.objects.only("id", "name", "slug", "owner_id").get(pk=organization.pk)
        loaded.name = "C"
        loaded.save()
    finally:
        post_save.disconnect(receiver, sender=Organization)

    assert seen and set(seen[-1]) == {"name", "slug", "owner_id"}
