# tests/performance/config.py
"""Configuration for Locust performance tests.

Reads from .env.test file in the same directory. All LOCUST_* variables
are defined there alongside the Django settings for docker-compose.
"""

from pathlib import Path

from decouple import Config as DecoupleConfig
from decouple import RepositoryEnv

# Load config from .env.test in the same directory as this file
_env_file = Path(__file__).parent / ".env.test"
_decouple_config = DecoupleConfig(RepositoryEnv(str(_env_file)))


class Config:
    """Performance test configuration."""

    # API endpoints
    BACKEND_URL: str = _decouple_config("LOCUST_BACKEND_URL", default="http://localhost:8000/api")
    MAILPIT_URL: str = _decouple_config("LOCUST_MAILPIT_URL", default="http://localhost:8025")

    # Email polling settings
    EMAIL_POLL_TIMEOUT: float = _decouple_config("LOCUST_EMAIL_POLL_TIMEOUT", default=10, cast=float)
    EMAIL_POLL_INTERVAL: float = _decouple_config("LOCUST_EMAIL_POLL_INTERVAL", default=0.5, cast=float)

    # Test user credentials (must pass Django validators - not too common, 8+ chars)
    DEFAULT_PASSWORD: str = _decouple_config("LOCUST_DEFAULT_PASSWORD", default="PerfTest-2026-Secure!")

    # Pre-seeded test data identifiers
    PERF_ORG_SLUG: str = "perf-test-org"
    PERF_ADMIN_EMAIL: str = "perf-admin@test.com"
    PERF_STAFF_EMAIL: str = "perf-staff@test.com"

    # Event slugs for different test scenarios
    PERF_RSVP_EVENT_SLUG: str = "perf-rsvp-event"
    PERF_RSVP_LIMITED_EVENT_SLUG: str = "perf-rsvp-limited-event"
    PERF_TICKET_FREE_EVENT_SLUG: str = "perf-ticket-free-event"
    PERF_TICKET_PWYC_EVENT_SLUG: str = "perf-ticket-pwyc-event"
    PERF_QUESTIONNAIRE_EVENT_SLUG: str = "perf-questionnaire-event"

    # Number of pre-seeded users
    NUM_PRESEEDED_USERS: int = _decouple_config("LOCUST_NUM_PRESEEDED_USERS", default=100, cast=int)

    # User email pattern: perf-user-{N}@test.com
    @classmethod
    def get_user_email(cls, index: int) -> str:
        """Get the email for a pre-seeded user by index."""
        return f"perf-user-{index}@test.com"

    # Request timeouts
    REQUEST_TIMEOUT: float = _decouple_config("LOCUST_REQUEST_TIMEOUT", default=30, cast=float)

    # Headers
    @classmethod
    def get_default_headers(cls, token: str | None = None) -> dict[str, str]:
        """Get default headers for API requests."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Language": "en",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers


# Singleton instance for easy access
config = Config()
