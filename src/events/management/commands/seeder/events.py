"""Event seeding module."""

import datetime
from datetime import timedelta

from django.utils import timezone

from events.management.commands.seeder.base import BaseSeeder
from events.models import Event, EventSeries
from events.models.mixins import ResourceVisibility
from geo.models import City

# Event series name templates
SERIES_NAMES = [
    "Monthly Meetup",
    "Weekly Workshop",
    "Quarterly Conference",
    "Annual Gala",
    "Tech Talk Series",
    "Art Exhibition Series",
    "Music Festival",
    "Community Gathering",
    "Professional Development",
    "Social Networking",
]

# Status and visibility maps (module-level constants to reduce complexity)
EVENT_STATUS_MAP = {
    "open": Event.EventStatus.OPEN,
    "closed": Event.EventStatus.CLOSED,
    "draft": Event.EventStatus.DRAFT,
    "cancelled": Event.EventStatus.CANCELLED,
}

EVENT_VISIBILITY_MAP = {
    "public": Event.Visibility.PUBLIC,
    "private": Event.Visibility.PRIVATE,
    "members_only": Event.Visibility.MEMBERS_ONLY,
    "staff_only": Event.Visibility.STAFF_ONLY,
}


class EventSeeder(BaseSeeder):
    """Seeder for events and event series."""

    def seed(self) -> None:
        """Seed events and event series."""
        self._create_event_series()
        self._create_events()

    def _create_event_series(self) -> None:
        """Create 2-5 event series per organization."""
        self.log("Creating event series...")

        series_to_create: list[EventSeries] = []

        for org in self.state.organizations:
            num_series = self.random_int(2, 5)
            series_names = self.random_sample(SERIES_NAMES, num_series)

            for i, name in enumerate(series_names):
                series = EventSeries(
                    organization=org,
                    name=f"{name} {i}",
                    slug=f"series-{i}",
                    description=self.faker.paragraph() if self.random_bool(0.7) else "",
                )
                series_to_create.append(series)

        self.state.event_series = self.batch_create(EventSeries, series_to_create, desc="Creating event series")
        self.log(f"  Created {len(self.state.event_series)} event series")

    def _get_event_timing(self, now: datetime.datetime) -> tuple[datetime.datetime, datetime.datetime]:
        """Generate event start and end times."""
        is_past = self.random_bool(self.config.past_event_pct)
        if is_past:
            start = now - timedelta(days=self.random_int(1, 90))
        else:
            start = now + timedelta(days=self.random_int(1, 120))
        end = start + timedelta(hours=self.random_int(2, 8))
        return start, end

    def _get_event_deadlines(
        self, start: datetime.datetime, requires_ticket: bool, status: Event.EventStatus
    ) -> dict[str, datetime.datetime | None]:
        """Generate event deadlines and check-in windows."""
        deadlines: dict[str, datetime.datetime | None] = {
            "rsvp_before": None,
            "apply_before": None,
            "check_in_starts_at": None,
            "check_in_ends_at": None,
        }
        if not requires_ticket and self.random_bool(0.5):
            deadlines["rsvp_before"] = start - timedelta(days=self.random_int(1, 7))
        if self.random_bool(0.3):
            deadlines["apply_before"] = start - timedelta(days=self.random_int(1, 14))
        if status == Event.EventStatus.OPEN and self.random_bool(0.6):
            deadlines["check_in_starts_at"] = start - timedelta(hours=1)
            deadlines["check_in_ends_at"] = start + timedelta(hours=self.random_int(2, 8))
        return deadlines

    def _create_events(self) -> None:
        """Create events with maximum edge case coverage."""
        self.log(f"Creating {self.config.num_events_per_org} events per org...")

        events_to_create: list[Event] = []
        now = timezone.now()
        cities = list(City.objects.all()[:50])

        # Group event series by organization
        org_series: dict[str, list[EventSeries]] = {}
        for series in self.state.event_series:
            org_id = str(series.organization_id)
            org_series.setdefault(org_id, []).append(series)

        for org in self.state.organizations:
            org_venues = self.state.venues.get(org.id, [])
            org_event_series = org_series.get(str(org.id), [])
            org_events: list[Event] = []

            for i in range(self.config.num_events_per_org):
                status = EVENT_STATUS_MAP[self.weighted_choice(self.config.event_status_weights)]
                visibility = EVENT_VISIBILITY_MAP[self.weighted_choice(self.config.visibility_weights)]
                start, end = self._get_event_timing(now)
                requires_ticket = self.random_bool(0.7)
                deadlines = self._get_event_deadlines(start, requires_ticket, status)

                event = Event(
                    organization=org,
                    name=f"{self.faker.catch_phrase()} {i}",
                    slug=f"event-{i}",
                    description=self.faker.paragraph() if self.random_bool(0.8) else "",
                    status=status,
                    visibility=visibility,
                    event_type=self.random_choice(list(Event.EventType.values)),
                    start=start,
                    end=end,
                    max_attendees=self.random_choice([0, 10, 25, 50, 100, 200, 500]),
                    requires_ticket=requires_ticket,
                    requires_full_profile=self.random_bool(0.2),
                    waitlist_open=self.random_bool(self.config.waitlist_event_pct),
                    potluck_open=self.random_bool(self.config.potluck_event_pct),
                    accept_invitation_requests=self.random_bool(0.4),
                    can_attend_without_login=self.random_bool(0.6),
                    address_visibility=self.random_choice(list(ResourceVisibility.values)),
                    max_tickets_per_user=self.random_choice([None, 1, 2, 5, 10]),
                    city=self.random_choice(cities) if cities else None,
                    address=self.faker.address() if self.random_bool(0.7) else None,
                    venue=self.random_choice(org_venues) if org_venues and self.random_bool(0.5) else None,
                    event_series=self.random_choice(org_event_series)
                    if org_event_series and self.random_bool(0.3)
                    else None,
                    rsvp_before=deadlines["rsvp_before"],
                    apply_before=deadlines["apply_before"],
                    check_in_starts_at=deadlines["check_in_starts_at"],
                    check_in_ends_at=deadlines["check_in_ends_at"],
                )
                events_to_create.append(event)
                org_events.append(event)

            self.state.org_events[org.id] = org_events

        self.state.events = self.batch_create(Event, events_to_create, desc="Creating events")

        # Update org_events with actual created events (with IDs)
        idx = 0
        for org in self.state.organizations:
            num_events = len(self.state.org_events.get(org.id, []))
            self.state.org_events[org.id] = self.state.events[idx : idx + num_events]
            idx += num_events

        self._categorize_events()
        self.log(f"  Created {len(self.state.events)} events")

    def _categorize_events(self) -> None:
        """Categorize events for edge case tracking."""
        for event in self.state.events:
            # Track by characteristics
            if event.waitlist_open:
                self.state.waitlist_events.append(event)

            if event.potluck_open:
                self.state.potluck_events.append(event)

            if event.start < timezone.now():
                self.state.past_events.append(event)

            if event.requires_ticket:
                self.state.ticketed_events.append(event)
            else:
                self.state.non_ticketed_events.append(event)

            if event.visibility == Event.Visibility.PRIVATE:
                self.state.private_events.append(event)

        self.log(f"  Waitlist events: {len(self.state.waitlist_events)}")
        self.log(f"  Potluck events: {len(self.state.potluck_events)}")
        self.log(f"  Past events: {len(self.state.past_events)}")
        self.log(f"  Ticketed events: {len(self.state.ticketed_events)}")
        self.log(f"  Non-ticketed events: {len(self.state.non_ticketed_events)}")
        self.log(f"  Private events: {len(self.state.private_events)}")
