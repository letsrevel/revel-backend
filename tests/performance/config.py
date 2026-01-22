# tests/performance/config.py
"""Configuration for Locust performance tests.

Environment variables (see .env.example):
    LOCUST_BACKEND_URL: Base URL for the Revel API (default: http://localhost:8000/api)
    LOCUST_MAILPIT_URL: Base URL for Mailpit (default: http://localhost:8025)
    LOCUST_DEFAULT_PASSWORD: Default password for test users (default: password123)
"""

from decouple import config as decouple_config


class Config:
    """Performance test configuration."""

    # API endpoints
    BACKEND_URL: str = decouple_config("LOCUST_BACKEND_URL", default="http://localhost:8000/api")
    MAILPIT_URL: str = decouple_config("LOCUST_MAILPIT_URL", default="http://localhost:8025")

    # Email polling settings
    EMAIL_POLL_TIMEOUT: float = decouple_config("LOCUST_EMAIL_POLL_TIMEOUT", default=10, cast=float)
    EMAIL_POLL_INTERVAL: float = decouple_config("LOCUST_EMAIL_POLL_INTERVAL", default=0.5, cast=float)

    # Test user credentials
    DEFAULT_PASSWORD: str = decouple_config("LOCUST_DEFAULT_PASSWORD", default="password123")

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
    NUM_PRESEEDED_USERS: int = decouple_config("LOCUST_NUM_PRESEEDED_USERS", default=100, cast=int)

    # User email pattern: perf-user-{N}@test.com
    @classmethod
    def get_user_email(cls, index: int) -> str:
        """Get the email for a pre-seeded user by index."""
        return f"perf-user-{index}@test.com"

    # Request timeouts
    REQUEST_TIMEOUT: float = decouple_config("LOCUST_REQUEST_TIMEOUT", default=30, cast=float)

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
