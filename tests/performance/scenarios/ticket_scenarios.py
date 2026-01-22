# tests/performance/scenarios/ticket_scenarios.py
"""Ticket checkout performance test scenarios.

These test BOTTLENECK endpoints:
- /events/{id}/tickets/{tier}/checkout
- /events/{id}/tickets/{tier}/checkout/pwyc
"""

from locust import task

from .base import AuthenticatedRevelUser


class FreeTicketUser(AuthenticatedRevelUser):
    """Scenario: Free ticket checkout.

    Tests the free ticket checkout flow (BOTTLENECK):
    1. GET /events/{id}/my-status
    2. GET /events/{id}/tickets/tiers
    3. POST /events/{id}/tickets/{tier}/checkout
    4. GET /events/{id}/my-status (verify)

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
            for tier in tiers:
                # Look for a free tier (price = 0 or payment_method = FREE)
                if tier.get("payment_method") == "FREE" or tier.get("price") == "0.00":
                    self._tier_id = tier.get("id")
                    break
            # If no free tier found, use first available
            if not self._tier_id and tiers:
                self._tier_id = tiers[0].get("id")

    @task(2)
    def check_status_and_tiers(self) -> None:
        """Check status and view tiers."""
        if not self._event_id:
            return
        self.api.get_my_status(self._event_id)
        self.api.get_ticket_tiers(self._event_id)

    @task(1)
    def checkout_free_ticket(self) -> None:
        """Checkout for a free ticket (BOTTLENECK)."""
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
            for tier in tiers:
                if tier.get("price_type") == "PWYC":
                    self._tier_id = tier.get("id")
                    # Extract price range if available
                    if tier.get("pwyc_min"):
                        self._min_price = float(tier["pwyc_min"])
                    if tier.get("pwyc_max"):
                        self._max_price = float(tier["pwyc_max"])
                    break
            # Fallback to first tier
            if not self._tier_id and tiers:
                self._tier_id = tiers[0].get("id")

    @task(2)
    def check_status_and_tiers(self) -> None:
        """Check status and view tiers."""
        if not self._event_id:
            return
        self.api.get_my_status(self._event_id)
        self.api.get_ticket_tiers(self._event_id)

    @task(1)
    def checkout_pwyc_ticket(self) -> None:
        """Checkout for a PWYC ticket (BOTTLENECK)."""
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
