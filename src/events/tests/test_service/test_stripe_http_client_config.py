"""Pin the global Stripe HTTP timeout config (issue #865 follow-up).

Every module that pins ``stripe.api_key``/``stripe.api_version`` at import
time also configures ``stripe.default_http_client`` with
``settings.STRIPE_HTTP_TIMEOUT_SECONDS``, so no outbound stripe-python call
(refund paths in particular — see docs/engineering-notes.md "Row locks
across Stripe calls") can hang anywhere near stripe-python's own ~80s
default. Importing either of the two primary sites is enough to prove the
client is configured, since ``stripe.default_http_client`` is a single
process-wide attribute on the ``stripe`` module.
"""

import stripe
from django.conf import settings

# Import side effect under test.
from events.service import stripe_service, stripe_webhooks  # noqa: F401


def test_default_http_client_is_configured_with_the_settings_timeout() -> None:
    """``stripe.default_http_client`` is a ``RequestsClient`` pinned to the setting."""
    assert isinstance(stripe.default_http_client, stripe.RequestsClient)  # type: ignore[attr-defined]
    assert stripe.default_http_client._timeout == settings.STRIPE_HTTP_TIMEOUT_SECONDS
