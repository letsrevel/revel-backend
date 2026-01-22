# tests/performance/scenarios/base.py
"""Base classes for Locust performance test scenarios."""

import logging
import typing as t

from clients.api_client import RevelAPIClient
from config import config
from data.generators import TestDataGenerator, get_data_generator
from locust import HttpUser, between

logger = logging.getLogger(__name__)


class RevelUserBase(HttpUser):
    """Base class for all Revel performance test users.

    Provides:
    - API client with auth handling
    - Test data generator
    - Common setup/teardown
    """

    # Default wait time between tasks (1-3 seconds)
    wait_time = between(1, 3)

    # Subclasses should set this to False
    abstract = True

    def __init__(self, *args: t.Any, **kwargs: t.Any) -> None:
        super().__init__(*args, **kwargs)
        self._api_client: RevelAPIClient | None = None
        self._data_generator: TestDataGenerator | None = None
        self._cached_event_ids: dict[str, str] = {}

    @property
    def api(self) -> RevelAPIClient:
        """Get the API client for this user.

        Lazily initializes the client on first access.
        """
        if self._api_client is None:
            self._api_client = RevelAPIClient(self)
        return self._api_client

    @property
    def data(self) -> TestDataGenerator:
        """Get the test data generator.

        Lazily initializes on first access.
        """
        if self._data_generator is None:
            self._data_generator = get_data_generator()
        return self._data_generator

    def on_start(self) -> None:
        """Called when a simulated user starts.

        Override in subclasses to perform login or setup.
        """
        pass

    def on_stop(self) -> None:
        """Called when a simulated user stops.

        Override in subclasses for cleanup.
        """
        pass

    def login_as_preseeded_user(self, index: int | None = None) -> bool:
        """Login as a pre-seeded test user.

        Args:
            index: User index, or None for random.

        Returns:
            True if login successful.
        """
        user = self.data.get_preseeded_user(index)
        return self.api.login(user.email, user.password)

    def login_as_random_preseeded_user(self) -> bool:
        """Login as a random pre-seeded user.

        Returns:
            True if login successful.
        """
        user = self.data.get_random_preseeded_user()
        return self.api.login(user.email, user.password)

    def get_event_id_by_slug(self, org_slug: str, event_slug: str) -> str | None:
        """Get event ID by slugs, with caching.

        Args:
            org_slug: Organization slug.
            event_slug: Event slug.

        Returns:
            Event UUID or None.
        """
        cache_key = f"{org_slug}/{event_slug}"
        if cache_key in self._cached_event_ids:
            return self._cached_event_ids[cache_key]

        event_data = self.api.get_event_by_slug(org_slug, event_slug)
        if event_data:
            event_id = event_data.get("id")
            self._cached_event_ids[cache_key] = event_id
            logger.info("Found event %s/%s -> %s", org_slug, event_slug, event_id)
            return event_id
        logger.error("Event not found: %s/%s", org_slug, event_slug)
        return None

    def get_perf_rsvp_event_id(self) -> str | None:
        """Get the RSVP test event ID."""
        return self.get_event_id_by_slug(
            config.PERF_ORG_SLUG,
            config.PERF_RSVP_EVENT_SLUG,
        )

    def get_perf_ticket_free_event_id(self) -> str | None:
        """Get the free ticket test event ID."""
        return self.get_event_id_by_slug(
            config.PERF_ORG_SLUG,
            config.PERF_TICKET_FREE_EVENT_SLUG,
        )

    def get_perf_ticket_pwyc_event_id(self) -> str | None:
        """Get the PWYC ticket test event ID."""
        return self.get_event_id_by_slug(
            config.PERF_ORG_SLUG,
            config.PERF_TICKET_PWYC_EVENT_SLUG,
        )

    def get_perf_questionnaire_event_id(self) -> str | None:
        """Get the questionnaire test event ID."""
        return self.get_event_id_by_slug(
            config.PERF_ORG_SLUG,
            config.PERF_QUESTIONNAIRE_EVENT_SLUG,
        )


class AuthenticatedRevelUser(RevelUserBase):
    """Base class for scenarios that require authentication.

    Automatically logs in as a random pre-seeded user on start.
    """

    abstract = True

    def on_start(self) -> None:
        """Login as a random pre-seeded user."""
        if not self.login_as_random_preseeded_user():
            # If login fails, stop this user
            self.environment.runner.quit()  # type: ignore[union-attr]


class AnonymousRevelUser(RevelUserBase):
    """Base class for scenarios that don't require authentication.

    Used for testing public endpoints.
    """

    abstract = True
