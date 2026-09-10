"""Pin the process-wide stripe-python configuration.

Every module that makes an outbound stripe-python call routes its setup through
``common.service.stripe_config.configure_stripe``, which pins ``stripe.api_key``,
``stripe.api_version`` and a ``stripe.default_http_client`` bounded by
``settings.STRIPE_HTTP_TIMEOUT_SECONDS`` — so no call (refund paths in particular, see
docs/engineering-notes.md "Row locks across Stripe calls") can hang anywhere near
stripe-python's own ~80s default, and no module depends on another module's import side
effects.
"""

import importlib

import pytest
import stripe
from django.conf import settings

from common.service.stripe_config import configure_stripe

# Import side effect under test (a module that configures at import time).
from events.service import stripe_service  # noqa: F401

# Every module that configures stripe-python. All but provision_stripe_webhooks do it at
# import time; that command calls configure_stripe() inside handle(), after the --dry-run
# early return, so a dry run stays free of any Stripe setup.
STRIPE_CONFIGURING_MODULES = [
    "accounts.tasks.payouts",
    "common.service.stripe_connect_service",
    "events.management.commands.provision_stripe_webhooks",
    "events.management.commands.scrub_stripe_products",
    "events.service.pending_checkout",
    "events.service.stripe_service",
    "events.service.stripe_webhooks",
    "events.service.subscription_stripe_base",
    "events.service.subscription_stripe_payloads",
    "events.service.subscription_stripe_service",
    "events.service.subscription_stripe_sync",
]


@pytest.mark.parametrize("module_path", STRIPE_CONFIGURING_MODULES)
def test_module_routes_stripe_setup_through_configure_stripe(module_path: str) -> None:
    """Each stripe-calling module imports the shared helper (lint proves it also uses it)."""
    module = importlib.import_module(module_path)
    assert getattr(module, "configure_stripe", None) is configure_stripe


def test_default_http_client_is_configured_with_the_settings_timeout() -> None:
    """Importing a configuring module is enough: the client is pinned to the setting."""
    assert isinstance(stripe.default_http_client, stripe.RequestsClient)
    assert stripe.default_http_client._timeout == settings.STRIPE_HTTP_TIMEOUT_SECONDS


def test_configure_stripe_pins_credentials_and_api_version() -> None:
    """``configure_stripe`` sets all three process-wide attributes and is idempotent."""
    configure_stripe()
    assert stripe.api_key == settings.STRIPE_SECRET_KEY
    assert stripe.api_version == settings.STRIPE_API_VERSION
    assert isinstance(stripe.default_http_client, stripe.RequestsClient)
    assert stripe.default_http_client._timeout == settings.STRIPE_HTTP_TIMEOUT_SECONDS
