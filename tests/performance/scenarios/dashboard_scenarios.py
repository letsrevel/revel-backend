# tests/performance/scenarios/dashboard_scenarios.py
"""Dashboard aggregation performance test scenarios."""

from locust import task
from scenarios.base import AuthenticatedRevelUser


class DashboardUser(AuthenticatedRevelUser):
    """Scenario: Dashboard heavy reads.

    Tests complex aggregation queries:
    1. GET /dashboard/events
    2. GET /dashboard/organizations
    3. GET /dashboard/tickets
    4. GET /dashboard/rsvps

    Weight: 15 (moderate - logged-in users)
    """

    abstract = False

    @task(3)
    def view_dashboard_events(self) -> None:
        """View dashboard events."""
        self.api.get_dashboard_events()

    @task(2)
    def view_dashboard_organizations(self) -> None:
        """View dashboard organizations."""
        self.api.get_dashboard_organizations()

    @task(2)
    def view_dashboard_tickets(self) -> None:
        """View dashboard tickets."""
        self.api.get_dashboard_tickets()

    @task(1)
    def view_dashboard_rsvps(self) -> None:
        """View dashboard RSVPs."""
        self.api.get_dashboard_rsvps()

    @task(1)
    def full_dashboard_load(self) -> None:
        """Simulate loading a full dashboard page.

        Fires all dashboard requests in sequence, similar to
        how a frontend might load the dashboard.
        """
        self.api.get_dashboard_events()
        self.api.get_dashboard_organizations()
        self.api.get_dashboard_tickets()
        self.api.get_dashboard_rsvps()
