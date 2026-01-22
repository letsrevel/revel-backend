# tests/performance/scenarios/discovery_scenarios.py
"""Event discovery performance test scenarios."""

import random

from locust import task

from .base import AnonymousRevelUser


class EventBrowser(AnonymousRevelUser):
    """Scenario: Anonymous event browsing.

    Tests public discovery endpoints:
    1. GET /events/ (list)
    2. GET /events/{id} (detail)
    3. GET /events/{id}/tickets/tiers

    Weight: 30 (high volume - typical user behavior)
    """

    abstract = False

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._discovered_event_ids: list[str] = []

    @task(5)
    def browse_events(self) -> None:
        """Browse the event listing."""
        page = random.randint(1, 3)
        result = self.api.list_events(page=page)

        if result and "items" in result:
            # Cache event IDs for later detail views
            for event in result["items"]:
                event_id = event.get("id")
                if event_id and event_id not in self._discovered_event_ids:
                    self._discovered_event_ids.append(event_id)

    @task(3)
    def view_event_detail(self) -> None:
        """View a random event's details."""
        if not self._discovered_event_ids:
            # Need to browse first
            self.browse_events()
            return

        event_id = random.choice(self._discovered_event_ids)
        self.api.get_event(event_id)

    @task(2)
    def view_ticket_tiers(self) -> None:
        """View ticket tiers for a random event."""
        if not self._discovered_event_ids:
            self.browse_events()
            return

        event_id = random.choice(self._discovered_event_ids)
        self.api.get_ticket_tiers(event_id)
