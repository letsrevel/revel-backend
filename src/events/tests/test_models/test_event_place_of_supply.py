"""Tests for ``Event.effective_vat_country`` place-of-supply resolution (#869).

Resolution order: explicit ``vat_country_code`` override → venue city →
event city → organization VAT country, always uppercased.
"""

import pytest
from django.contrib.gis.geos import Point

from events.models import Event, Organization, Venue
from geo.models import City

pytestmark = pytest.mark.django_db


def _make_city(iso2: str, city_id: int, name: str, lon: float, lat: float) -> City:
    """Create a City row with the minimum fields the model requires."""
    return City.objects.create(
        name=name,
        ascii_name=name,
        country=name,
        iso2=iso2,
        iso3=f"{iso2}X",
        city_id=city_id,
        location=Point(lon, lat),
        population=1000,
    )


@pytest.fixture
def berlin(db: None) -> City:
    """A German city."""
    return _make_city("DE", 900001, "Berlin", 13.40, 52.52)


@pytest.fixture
def rome(db: None) -> City:
    """An Italian city."""
    return _make_city("IT", 900002, "Rome", 12.49, 41.90)


class TestEffectiveVatCountry:
    """Resolution order and normalization of Event.effective_vat_country."""

    def test_explicit_override_wins_over_everything(
        self, event: Event, organization: Organization, berlin: City, rome: City
    ) -> None:
        """An explicit vat_country_code beats venue city, event city and org country."""
        organization.vat_country_code = "AT"
        organization.save(update_fields=["vat_country_code"])
        venue = Venue.objects.create(organization=organization, name="Hall", city=berlin)
        event.venue = venue
        event.city = rome
        event.vat_country_code = "FR"
        event.save(update_fields=["venue", "city", "vat_country_code"])

        assert event.effective_vat_country == "FR"

    def test_override_is_uppercased(self, event: Event) -> None:
        """A lowercase override is normalized to uppercase."""
        event.vat_country_code = "fr"
        event.save(update_fields=["vat_country_code"])

        assert event.effective_vat_country == "FR"

    def test_venue_city_beats_event_city_and_org(
        self, event: Event, organization: Organization, berlin: City, rome: City
    ) -> None:
        """Without an override, the venue's city drives the VAT country."""
        organization.vat_country_code = "AT"
        organization.save(update_fields=["vat_country_code"])
        venue = Venue.objects.create(organization=organization, name="Hall", city=berlin)
        event.venue = venue
        event.city = rome
        event.save(update_fields=["venue", "city"])

        assert event.effective_vat_country == "DE"

    def test_venue_without_city_falls_through_to_event_city(
        self, event: Event, organization: Organization, rome: City
    ) -> None:
        """A venue with no city does not short-circuit the chain."""
        venue = Venue.objects.create(organization=organization, name="Hall")
        event.venue = venue
        event.city = rome
        event.save(update_fields=["venue", "city"])

        assert event.effective_vat_country == "IT"

    def test_event_city_beats_org_country(self, event: Event, organization: Organization, berlin: City) -> None:
        """Without an override or venue, the event's own city wins over the org."""
        organization.vat_country_code = "AT"
        organization.save(update_fields=["vat_country_code"])
        event.city = berlin
        event.save(update_fields=["city"])

        assert event.effective_vat_country == "DE"

    def test_org_country_is_the_last_fallback_and_uppercased(self, event: Event, organization: Organization) -> None:
        """With no override, venue or city, the org's VAT country applies, uppercased."""
        organization.vat_country_code = "at"
        organization.save(update_fields=["vat_country_code"])

        assert event.effective_vat_country == "AT"

    def test_everything_empty_yields_empty_string(self, event: Event) -> None:
        """No override, no venue, no city, no org country → empty string."""
        assert event.venue is None
        assert event.city is None
        assert event.organization.vat_country_code == ""

        assert event.effective_vat_country == ""

    def test_city_iso2_is_uppercased(self, event: Event, organization: Organization) -> None:
        """A lowercase iso2 on the city row is normalized to uppercase."""
        city = _make_city("de", 900003, "Lowertown", 9.99, 50.0)
        event.city = city
        event.save(update_fields=["city"])

        assert event.effective_vat_country == "DE"
