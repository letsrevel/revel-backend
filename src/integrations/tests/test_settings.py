"""Settings-level checks for the integrations app."""

from django.apps import apps
from django.conf import settings

from revel.settings.integrations import eventbrite_enabled


def test_app_is_installed() -> None:
    assert apps.is_installed("integrations")


def test_state_ttl_is_timedelta() -> None:
    assert settings.INTEGRATIONS_CONNECT_STATE_TTL.total_seconds() == 600


def test_eventbrite_enabled_requires_both_values() -> None:
    assert eventbrite_enabled("id", "secret") is True
    assert eventbrite_enabled("", "secret") is False
    assert eventbrite_enabled("id", "") is False
