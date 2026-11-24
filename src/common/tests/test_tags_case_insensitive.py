"""Tags match regardless of letter case (#1517)."""

import typing as t
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from common.models import Tag, TagAssignment
from events.filters import EventFilterSchema, EventSeriesFilterSchema, OrganizationFilterSchema
from events.models import Event, EventSeries, Organization

pytestmark = pytest.mark.django_db


@pytest.fixture
def org(organization: Organization) -> Organization:
    """Organization fixture (shared conftest)."""
    return organization


def test_add_reuses_existing_tag_across_case_and_keeps_casing(org: Organization) -> None:
    """Adding "FUN" or "#Fun " reuses the tag "fun" instead of creating a duplicate."""
    org.add_tags("fun")
    org.add_tags("FUN", "#Fun ")

    assert list(Tag.objects.values_list("name", flat=True)) == ["fun"]
    assert [tag.name for tag in org.get_tags()] == ["fun"]
    assert TagAssignment.objects.count() == 1


def test_add_reuses_tag_created_by_another_object(org: Organization) -> None:
    """A tag first created with another casing is shared across objects."""
    series = EventSeries.objects.create(organization=org, name="S", slug="s")
    org.add_tags("Fun")
    series.add_tags("fun")

    assert Tag.objects.count() == 1
    assert Tag.objects.get().assignments.count() == 2


def test_remove_is_case_insensitive(org: Organization) -> None:
    """Removing "FUN" removes the tag "fun" and leaves the other tags alone."""
    org.add_tags("fun", "music", "other")

    org.remove_tags("FUN", " #MUSIC ")

    assert [tag.name for tag in org.get_tags()] == ["other"]


def test_unique_constraint_rejects_case_duplicate(org: Organization) -> None:
    """Saving refuses two tags that differ only by case."""
    Tag.objects.create(name="fun")

    with pytest.raises((IntegrityError, ValidationError)), transaction.atomic():
        Tag.objects.create(name="Fun")


def _make_event(org: Organization, series: EventSeries | None = None) -> Event:
    start = timezone.now() + timedelta(days=7)
    return Event.objects.create(
        organization=org,
        name="E",
        slug="e",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        event_series=series,
        status="open",
        start=start,
        end=start + timedelta(days=1),
    )


@pytest.mark.parametrize("query", ["fun", "FUN", "#Fun ", ["fun"]])
def test_organization_filter_is_case_insensitive(org: Organization, query: t.Any) -> None:
    """The organization tag filter matches regardless of case, ``#`` and spaces."""
    org.add_tags("fun")
    tags = [query] if isinstance(query, str) else query

    found = Organization.objects.filter(OrganizationFilterSchema(tags=tags).get_filter_expression())

    assert list(found) == [org]


def test_event_filter_matches_event_org_and_series_tags_case_insensitively(org: Organization) -> None:
    """The event tag filter checks the event, its organization and its series."""
    series = EventSeries.objects.create(organization=org, name="S", slug="s")
    event = _make_event(org, series)
    schema = EventFilterSchema(tags=["FUN"])

    event.add_tags("fun")
    assert list(Event.objects.filter(schema.get_filter_expression())) == [event]
    event.clear_tags()

    org.add_tags("Fun")
    assert list(Event.objects.filter(schema.get_filter_expression())) == [event]
    org.clear_tags()

    series.add_tags("fUn")
    assert list(Event.objects.filter(schema.get_filter_expression())) == [event]


def test_series_filter_is_case_insensitive(org: Organization) -> None:
    """The series tag filter matches the series and its organization regardless of case."""
    series = EventSeries.objects.create(organization=org, name="S", slug="s")
    series.add_tags("fun")

    assert list(EventSeries.objects.filter(EventSeriesFilterSchema(tags=["FUN"]).get_filter_expression())) == [series]


def test_series_filter_matches_organization_only_tag(org: Organization) -> None:
    """A series is found when only its organization has the tag."""
    series = EventSeries.objects.create(organization=org, name="S", slug="s")
    org.add_tags("Fun")

    assert list(EventSeries.objects.filter(EventSeriesFilterSchema(tags=["FUN"]).get_filter_expression())) == [series]


def test_final_sigma_is_folded_by_the_database(org: Organization) -> None:
    """Python and PostgreSQL fold the final sigma differently; the database decides."""
    org.add_tags("ΧΟΡΟΣ")

    found = Organization.objects.filter(OrganizationFilterSchema(tags=["ΧΟΡΟΣ"]).get_filter_expression())
    assert list(found) == [org]

    org.remove_tags("ΧΟΡΟΣ")
    assert org.get_tags() == []


def test_add_reuses_tag_that_only_the_database_folds_equal(org: Organization) -> None:
    """The Kelvin sign (U+212A) lowercases to "k" in the database, so it reuses the tag "k"."""
    org.add_tags("k")

    org.add_tags("\u212a")

    assert Tag.objects.count() == 1


def test_filter_without_match_finds_nothing(org: Organization) -> None:
    """A tag that nobody uses matches no object, and a blank tag matches nothing."""
    org.add_tags("fun")

    for tags in (["other"], [" # "]):
        assert not Organization.objects.filter(OrganizationFilterSchema(tags=tags).get_filter_expression()).exists()


def test_database_constraint_rejects_case_duplicate_bypassing_validation() -> None:
    """The database itself refuses a case-duplicate when ``save()`` validation is bypassed."""
    Tag.objects.create(name="fun")

    with pytest.raises(IntegrityError), transaction.atomic():
        Tag.objects.bulk_create([Tag(name="FUN")])
