"""Tests for the Organization/Event admin additions: city, open link, manual counts, origin filter."""

import datetime as dt
import typing as t

import pytest
from django.contrib.gis.geos import Point
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.admin.base import format_city, frontend_url, view_on_revel_link
from events.admin.organization import OrganizationAdmin
from events.models import Event, Organization, Venue
from geo.models import City

pytestmark = pytest.mark.django_db

NO_MANIFEST_STORAGE = override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)


def _city(name: str = "Vienna", country: str = "AT", city_id: int = 1) -> City:
    return City.objects.create(
        name=name, ascii_name=name, country=country, city_id=city_id, location=Point(16.37, 48.21)
    )


def _event(org: Organization, slug: str, **kwargs: t.Any) -> Event:
    defaults: dict[str, t.Any] = {
        "organization": org,
        "name": slug,
        "slug": slug,
        "event_type": Event.EventType.PUBLIC,
        "visibility": Event.Visibility.PUBLIC,
        "max_attendees": 100,
        "status": Event.EventStatus.OPEN,
        "start": timezone.now(),
        "requires_ticket": True,
    }
    defaults.update(kwargs)
    if defaults.get("is_template"):
        defaults["status"] = Event.EventStatus.DRAFT
    return Event.objects.create(**defaults)


@pytest.fixture
def org(revel_user_factory: RevelUserFactory) -> Organization:
    return Organization.objects.create(name="Acme", slug="acme", owner=revel_user_factory(), city=_city())


def test_format_city_and_url_helpers() -> None:
    assert format_city(None) == "—"
    assert format_city(_city()) == "Vienna (AT)"
    assert frontend_url("/org/x").endswith("/org/x")
    assert view_on_revel_link(None) == "—"
    assert 'target="_blank"' in view_on_revel_link("https://x.test/a")


@NO_MANIFEST_STORAGE
def test_org_changelist_columns(admin_client: Client, org: Organization) -> None:
    _event(org, "manual")
    _event(org, "occ", occurrence_index=0)
    _event(org, "tpl", is_template=True)

    response = admin_client.get(reverse("admin:events_organization_changelist"))

    assert response.status_code == 200
    html = response.content.decode()
    assert "Vienna (AT)" in html
    assert "/org/acme" in html
    assert "1 / 2" in html  # manual / all (templates excluded)
    assert "Last created" in html


@NO_MANIFEST_STORAGE
def test_org_changelist_sortable_by_manual_count(
    admin_client: Client, org: Organization, revel_user_factory: RevelUserFactory
) -> None:
    """Sorting by the Events column orders by the manual count, not the total."""
    busy = Organization.objects.create(name="Busy", slug="busy", owner=revel_user_factory())
    _event(busy, "b1")
    _event(busy, "b2")
    _event(org, "a1")
    for i in range(3):  # more events in total, but generated ones
        _event(org, f"occ{i}", occurrence_index=i)
    col = OrganizationAdmin.list_display.index("events_count") + 1
    url = reverse("admin:events_organization_changelist")

    asc = admin_client.get(f"{url}?o={col}").content.decode()
    desc = admin_client.get(f"{url}?o=-{col}").content.decode()

    assert asc.index("/org/acme") < asc.index("/org/busy")
    assert desc.index("/org/busy") < desc.index("/org/acme")


@NO_MANIFEST_STORAGE
def test_org_change_page_shows_id_and_link(admin_client: Client, org: Organization) -> None:
    response = admin_client.get(reverse("admin:events_organization_change", args=[org.pk]))
    assert response.status_code == 200
    html = response.content.decode()
    assert str(org.pk) in html
    assert "View on Revel" in html
    assert "/org/acme" in html


@NO_MANIFEST_STORAGE
def test_org_add_page_renders(admin_client: Client) -> None:
    response = admin_client.get(reverse("admin:events_organization_add"))
    assert response.status_code == 200
    assert "View ↗" not in response.content.decode()  # no link to an org that doesn't exist yet


@NO_MANIFEST_STORAGE
@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("manual", {"manual"}),
        ("occurrence", {"occ"}),
        ("template", {"tpl"}),
        (None, {"manual", "occ", "tpl"}),
    ],
)
def test_event_origin_filter(admin_client: Client, org: Organization, origin: str | None, expected: set[str]) -> None:
    _event(org, "manual")
    _event(org, "occ", occurrence_index=0)
    _event(org, "tpl", is_template=True)
    query = f"?origin={origin}" if origin else ""

    response = admin_client.get(f"{reverse('admin:events_event_changelist')}{query}")

    assert response.status_code == 200
    assert response.context_data is not None
    assert {e.slug for e in response.context_data["cl"].queryset} == expected


@NO_MANIFEST_STORAGE
def test_event_changelist_city_origin_and_link(admin_client: Client, org: Organization) -> None:
    venue = Venue.objects.create(organization=org, name="Hall", city=_city("Graz", "AT", 2))
    event = _event(org, "at-venue", venue=venue)
    _event(org, "tpl", is_template=True)

    html = admin_client.get(reverse("admin:events_event_changelist")).content.decode()

    assert "Graz (AT)" in html  # venue city fallback
    assert f"/events/{event.pk}" in html
    assert "Template" in html
    assert "Manual" in html


@NO_MANIFEST_STORAGE
def test_event_city_prefers_own_city(admin_client: Client, org: Organization) -> None:
    venue = Venue.objects.create(organization=org, name="Hall", city=_city("Graz", "AT", 2))
    _event(org, "e", venue=venue, city=_city("Linz", "AT", 3))

    html = admin_client.get(reverse("admin:events_event_changelist")).content.decode()

    assert "Linz (AT)" in html


@NO_MANIFEST_STORAGE
def test_event_change_page_shows_id_and_link(admin_client: Client, org: Organization) -> None:
    event = _event(org, "e")
    html = admin_client.get(reverse("admin:events_event_change", args=[event.pk])).content.decode()
    assert str(event.pk) in html
    assert "View on Revel" in html
    assert f"/events/{event.pk}" in html


@NO_MANIFEST_STORAGE
def test_event_add_page_renders(admin_client: Client, superuser: RevelUser) -> None:
    response = admin_client.get(reverse("admin:events_event_add"))
    assert response.status_code == 200
    assert "View ↗" not in response.content.decode()  # no link to an event that doesn't exist yet


@NO_MANIFEST_STORAGE
def test_created_at_range_filter_includes_whole_to_day(admin_client: Client, org: Organization) -> None:
    """An event created on the afternoon of the "to" date is inside the range (Unfold compares to midnight)."""
    afternoon = _event(org, "afternoon")
    next_day = _event(org, "next-day")
    Event.objects.filter(pk=afternoon.pk).update(created_at=timezone.make_aware(dt.datetime(2026, 3, 10, 15, 30)))
    Event.objects.filter(pk=next_day.pk).update(created_at=timezone.make_aware(dt.datetime(2026, 3, 11, 0, 30)))
    url = reverse("admin:events_event_changelist")

    html = admin_client.get(f"{url}?created_at_from=2026-03-10&created_at_to=2026-03-10").content.decode()

    assert f"/events/{afternoon.pk}" in html
    assert f"/events/{next_day.pk}" not in html
