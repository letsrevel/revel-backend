# src/events/management/commands/bootstrap_helpers/base.py
"""Base classes and shared state for bootstrap helpers."""

from dataclasses import dataclass, field

from faker import Faker

from accounts.models import RevelUser
from common.models import Tag
from events import models as events_models
from geo.models import City


@dataclass
class BootstrapState:
    """Shared state container for bootstrap process."""

    fake: Faker = field(default_factory=lambda: Faker("en_US"))
    users: dict[str, RevelUser] = field(default_factory=dict)
    orgs: dict[str, events_models.Organization] = field(default_factory=dict)
    venues: dict[str, events_models.Venue] = field(default_factory=dict)
    events: dict[str, events_models.Event] = field(default_factory=dict)
    event_series: dict[str, events_models.EventSeries] = field(default_factory=dict)
    tags: dict[str, Tag] = field(default_factory=dict)
    cities: dict[str, City] = field(default_factory=dict)

    def fake_address(self) -> str:
        """Generate a clean fake address."""
        return " ".join(self.fake.address().split())

    def load_cities(self) -> None:
        """Load cities for events."""
        self.cities["vienna"] = City.objects.get(name="Vienna", country="Austria")
        new_york = City.objects.filter(name="New York", country="United States").first()
        london = City.objects.filter(name="London", country="United Kingdom").first()
        berlin = City.objects.filter(name="Berlin", country="Germany").first()
        tokyo = City.objects.filter(name="Tokyo", country="Japan").first()

        assert new_york is not None, "New York city not found"
        assert london is not None, "London city not found"
        assert berlin is not None, "Berlin city not found"
        assert tokyo is not None, "Tokyo city not found"

        self.cities["new_york"] = new_york
        self.cities["london"] = london
        self.cities["berlin"] = berlin
        self.cities["tokyo"] = tokyo
