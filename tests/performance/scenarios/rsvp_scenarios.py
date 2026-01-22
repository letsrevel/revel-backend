# tests/performance/scenarios/rsvp_scenarios.py
"""RSVP flow performance test scenarios.

These test BOTTLENECK endpoints:
- /events/{id}/my-status
- /events/{id}/rsvp/{status}
"""

from locust import task

from .base import AuthenticatedRevelUser


class RSVPUser(AuthenticatedRevelUser):
    """Scenario: RSVP to events.

    Tests the RSVP flow (BOTTLENECK endpoints):
    1. GET /events/{id}/my-status
    2. POST /events/{id}/rsvp/yes
    3. GET /events/{id}/my-status (verify)

    Weight: 15 (bottleneck testing)
    """

    abstract = False

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._event_id: str | None = None

    def on_start(self) -> None:
        """Login and cache event ID."""
        super().on_start()
        self._event_id = self.get_perf_rsvp_event_id()

    @task(3)
    def check_my_status(self) -> None:
        """Check status for the RSVP event (BOTTLENECK)."""
        if not self._event_id:
            return
        self.api.get_my_status(self._event_id)

    @task(2)
    def rsvp_yes(self) -> None:
        """RSVP yes to the event (BOTTLENECK)."""
        if not self._event_id:
            return
        self.api.rsvp(self._event_id, "yes")

    @task(1)
    def full_rsvp_flow(self) -> None:
        """Execute full RSVP flow.

        1. Check status
        2. RSVP yes
        3. Verify status changed
        """
        if not self._event_id:
            return

        # Check current status
        self.api.get_my_status(self._event_id)

        # RSVP
        self.api.rsvp(self._event_id, "yes")

        # Verify
        self.api.get_my_status(self._event_id)
