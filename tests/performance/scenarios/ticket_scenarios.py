# tests/performance/scenarios/ticket_scenarios.py
"""Ticket checkout performance test scenarios.

These test BOTTLENECK endpoints:
- /events/{id}/tickets/{tier}/checkout
- /events/{id}/tickets/{tier}/checkout/pwyc
"""

import logging

from locust import task
from scenarios.base import AuthenticatedRevelUser

logger = logging.getLogger(__name__)


class FreeTicketUser(AuthenticatedRevelUser):
    """Scenario: Free ticket checkout.

    Tests the free ticket checkout flow (BOTTLENECK):
    1. GET /events/{id}/my-status
    2. GET /events/{id}/tickets/tiers
    3. POST /events/{id}/tickets/{tier}/checkout
    4. GET /events/{id}/my-status (verify)

    Users can purchase multiple tickets (max_tickets_per_user=None on event).
    Each checkout uses a unique guest name for continuous load testing.

    Weight: 10 (bottleneck testing)
    """

    abstract = False

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._event_id: str | None = None
        self._tier_id: str | None = None

    def on_start(self) -> None:
        """Login and cache event/tier IDs."""
        super().on_start()
        self._event_id = self.get_perf_ticket_free_event_id()

        # Get the free tier ID
        if self._event_id:
            tiers = self.api.get_ticket_tiers(self._event_id)
            logger.info("Free ticket tiers for event %s: %s", self._event_id, tiers)
            for tier in tiers:
                # Look for a free tier (payment_method = "free")
                if tier.get("payment_method") == "free":
                    self._tier_id = tier.get("id")
                    break
            if not self._tier_id:
                logger.error(
                    "No free tier found for event %s. Available tiers: %s",
                    self._event_id,
                    [(t.get("id"), t.get("payment_method"), t.get("price_type")) for t in tiers],
                )

    @task(2)
    def check_status_and_tiers(self) -> None:
        """Check status and view tiers."""
        if not self._event_id:
            return
        self.api.get_my_status(self._event_id)
        self.api.get_ticket_tiers(self._event_id)

    @task(1)
    def checkout_free_ticket(self) -> None:
        """Checkout for a free ticket (BOTTLENECK).

        Purchases a ticket with unique guest name each time.
        max_tickets_per_user=None allows unlimited purchases.
        """
        if not self._event_id or not self._tier_id:
            return

        guest_name = self.data.generate_guest_name()
        self.api.checkout_free(self._event_id, self._tier_id, guest_name)

    @task(1)
    def full_checkout_flow(self) -> None:
        """Execute full checkout flow.

        1. Check status
        2. View tiers
        3. Checkout
        4. Verify status
        """
        if not self._event_id or not self._tier_id:
            return

        self.api.get_my_status(self._event_id)
        self.api.get_ticket_tiers(self._event_id)

        guest_name = self.data.generate_guest_name()
        self.api.checkout_free(self._event_id, self._tier_id, guest_name)

        self.api.get_my_status(self._event_id)


class PWYCTicketUser(AuthenticatedRevelUser):
    """Scenario: PWYC ticket checkout.

    Tests the PWYC checkout flow (BOTTLENECK):
    1. GET /events/{id}/my-status
    2. GET /events/{id}/tickets/tiers
    3. POST /events/{id}/tickets/{tier}/checkout/pwyc
    4. GET /events/{id}/my-status (verify)

    Users can purchase multiple tickets (max_tickets_per_user=None on event).
    Each checkout uses a unique guest name for continuous load testing.

    Weight: 5 (bottleneck testing, less common)
    """

    abstract = False

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._event_id: str | None = None
        self._tier_id: str | None = None
        self._min_price: float = 5.0
        self._max_price: float = 50.0

    def on_start(self) -> None:
        """Login and cache event/tier IDs."""
        super().on_start()
        self._event_id = self.get_perf_ticket_pwyc_event_id()

        # Get the PWYC tier ID
        if self._event_id:
            tiers = self.api.get_ticket_tiers(self._event_id)
            logger.info("PWYC ticket tiers for event %s: %s", self._event_id, tiers)
            for tier in tiers:
                if tier.get("price_type") == "pwyc":
                    self._tier_id = tier.get("id")
                    # Extract price range if available
                    if tier.get("pwyc_min"):
                        self._min_price = float(tier["pwyc_min"])
                    if tier.get("pwyc_max"):
                        self._max_price = float(tier["pwyc_max"])
                    break
            if not self._tier_id:
                logger.error(
                    "No PWYC tier found for event %s. Available tiers: %s",
                    self._event_id,
                    [(t.get("id"), t.get("payment_method"), t.get("price_type")) for t in tiers],
                )

    @task(2)
    def check_status_and_tiers(self) -> None:
        """Check status and view tiers."""
        if not self._event_id:
            return
        self.api.get_my_status(self._event_id)
        self.api.get_ticket_tiers(self._event_id)

    @task(1)
    def checkout_pwyc_ticket(self) -> None:
        """Checkout for a PWYC ticket (BOTTLENECK).

        Purchases a ticket with unique guest name each time.
        max_tickets_per_user=None allows unlimited purchases.
        """
        if not self._event_id or not self._tier_id:
            return

        guest_name = self.data.generate_guest_name()
        price = self.data.generate_pwyc_amount(self._min_price, self._max_price)
        self.api.checkout_pwyc(self._event_id, self._tier_id, guest_name, price)

    @task(1)
    def full_pwyc_checkout_flow(self) -> None:
        """Execute full PWYC checkout flow."""
        if not self._event_id or not self._tier_id:
            return

        self.api.get_my_status(self._event_id)
        self.api.get_ticket_tiers(self._event_id)

        guest_name = self.data.generate_guest_name()
        price = self.data.generate_pwyc_amount(self._min_price, self._max_price)
        self.api.checkout_pwyc(self._event_id, self._tier_id, guest_name, price)

        self.api.get_my_status(self._event_id)
