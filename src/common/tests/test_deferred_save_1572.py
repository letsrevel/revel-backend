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

    assert seen
    written = set(seen[-1])
    assert {"name", "slug", "owner_id"} <= written
    assert not written & {"description", "updated_at", "logo"}


def test_a_field_that_a_later_save_step_sets_is_written(organization: Organization) -> None:
    """``LocationMixin`` runs after ``TimeStampedModel`` in the MRO and sets ``location`` from the city."""
    from django.contrib.gis.geos import Point

    from geo.models import City

    city = City.objects.create(
        name="C1572",
        ascii_name="C1572",
        country="AT",
        iso2="AT",
        iso3="AUT",
        city_id=1572,
        location=Point(16.37, 48.21),
    )
    Organization.objects.filter(pk=organization.pk).update(city=None, location=None)
    loaded = Organization.objects.only("id", "name", "slug", "owner_id", "city_id").get(pk=organization.pk)
    loaded.city = city

    loaded.save()

    organization.refresh_from_db()
    assert organization.city_id == city.pk
    assert organization.location is not None


def test_positional_update_fields_still_works(organization: Organization) -> None:
    """A positional ``update_fields`` must not get a second, keyword ``update_fields``."""
    import warnings

    loaded: t.Any = Organization.objects.only("id", "name", "slug", "owner_id").get(pk=organization.pk)
    loaded.name = "Positional"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # Django 5.2 deprecates positional save() arguments
        loaded.save(False, False, None, ["name"])

    organization.refresh_from_db()
    assert organization.name == "Positional"


def test_a_deferred_save_does_not_read_each_deferred_field(organization: Organization) -> None:
    """The constraint and unique checks must not load every deferred field, one query each."""
    event = Event.objects.create(organization=organization, name="E", slug="e-1572-q", start=timezone.now())
    loaded = Event.objects.only("id", "name", "organization_id", "start").get(pk=event.pk)
    loaded.name = "R"

    with CaptureQueriesContext(connection) as queries:
        loaded.save()

    read_columns = {
        m.group(1)
        for q in queries.captured_queries
        if (m := re.match(r'SELECT "events_event"\."id", "events_event"\."(\w+)" FROM', q["sql"]))
    }
    # Event.save and Event.clean read some fields for their rules; no rule reads these.
    assert not read_columns & {"description", "logo", "cover_art", "invitation_message"}, read_columns


def test_a_deferred_foreign_key_that_clean_reads_is_not_written(organization: Organization) -> None:
    """``Event.clean`` reads ``event_series``: its cached object must not make Django write the column.

    The event needs a series: Django skips a cached ``None`` before the write.
    """
    from events.models import EventSeries

    series = EventSeries.objects.create(organization=organization, name="S", slug="s-1572")
    event = Event.objects.create(
        organization=organization, event_series=series, name="E", slug="e-1572-fk", start=timezone.now()
    )
    loaded = Event.objects.only("id", "name", "organization_id", "start").get(pk=event.pk)
    loaded.name = "R"

    with CaptureQueriesContext(connection) as queries:
        loaded.save()

    [update] = _update_sql(queries, "events_event")
    assert '"event_series_id"' not in update


def test_a_failed_check_still_defers_the_fields_it_read(organization: Organization) -> None:
    """``Event.clean`` runs after a field error and reads ``event_series``: a retry must not write that column."""
    from django.core.exceptions import ValidationError

    from events.models import EventSeries

    series = EventSeries.objects.create(organization=organization, name="S", slug="s-1572-retry")
    event = Event.objects.create(
        organization=organization, event_series=series, name="E", slug="e-1572-retry", start=timezone.now()
    )
    loaded = Event.objects.only("id", "name", "organization_id", "start").get(pk=event.pk)
    loaded.name = ""

    with pytest.raises(ValidationError):
        loaded.save()

    assert "event_series_id" in loaded.get_deferred_fields()
    loaded.name = "Fixed"
    with CaptureQueriesContext(connection) as queries:
        loaded.save()
    [update] = _update_sql(queries, "events_event")
    assert '"event_series_id"' not in update
