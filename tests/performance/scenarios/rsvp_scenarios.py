# tests/performance/scenarios/rsvp_scenarios.py
"""RSVP flow performance test scenarios.

These test BOTTLENECK endpoints:
- /events/{id}/my-status
- /events/{id}/rsvp/{status}
"""

import itertools
import logging

from locust import task
from scenarios.base import AuthenticatedRevelUser

logger = logging.getLogger(__name__)


class RSVPUser(AuthenticatedRevelUser):
    """Scenario: RSVP to events with status alternation.

    Tests the RSVP flow (BOTTLENECK endpoints):
    1. GET /events/{id}/my-status
    2. POST /events/{id}/rsvp/{status}
    3. GET /events/{id}/my-status (verify)

    To properly stress-test eligibility checks, we alternate RSVP status:
    - no -> yes (RUNS FULL ELIGIBILITY CHECK)
    - yes -> maybe (bypasses eligibility - user already has yes)
    - maybe -> yes (RUNS FULL ELIGIBILITY CHECK)

    This ensures eligibility logic is exercised on every "yes" transition.

    Weight: 15 (bottleneck testing)
    """

    abstract = False

    # Status cycle: every "yes" after non-yes triggers full eligibility check
    _STATUS_CYCLE = ["no", "yes", "maybe", "yes"]

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._event_id: str | None = None
        self._status_iterator = itertools.cycle(self._STATUS_CYCLE)

    def on_start(self) -> None:
        """Login and cache event ID."""
        super().on_start()
        self._event_id = self.get_perf_rsvp_event_id()

    def _get_next_status(self) -> str:
        """Get next status in the cycle."""
        return next(self._status_iterator)

    @task(3)
    def check_my_status(self) -> None:
        """Check status for the RSVP event (BOTTLENECK)."""
        if not self._event_id:
            return
        self.api.get_my_status(self._event_id)

    @task(2)
    def rsvp_cycle(self) -> None:
        """RSVP with alternating status to stress eligibility checks (BOTTLENECK)."""
        if not self._event_id:
            return
        status = self._get_next_status()
        success = self.api.rsvp(self._event_id, status)
        if not success:
            logger.error(
                "RSVP failed: event=%s, status=%s",
                self._event_id,
                status,
            )

    @task(1)
    def full_rsvp_flow(self) -> None:
        """Execute full RSVP flow with status change.

        1. Check status
        2. RSVP with next status (alternating)
        3. Verify status changed
        """
        if not self._event_id:
            return

        # Check current status
        self.api.get_my_status(self._event_id)

        # RSVP with next status in cycle
        status = self._get_next_status()
        self.api.rsvp(self._event_id, status)

        # Verify
        self.api.get_my_status(self._event_id)
