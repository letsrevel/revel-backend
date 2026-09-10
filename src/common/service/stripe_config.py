"""Process-wide stripe-python configuration.

See docs/engineering-notes.md "Row locks across Stripe calls" for why the HTTP
timeout in particular matters.
"""

import stripe
from django.conf import settings


def configure_stripe() -> None:
    """Pin credentials, API version and the HTTP timeout on the ``stripe`` module.

    Called at import time by every module that makes an outbound stripe-python call, so no
    module depends on another module's import side effects. The pinned API version guards
    outbound response shapes against silent changes when the stripe SDK (whose default version
    tracks its release) gets bumped by a ``uv sync``. The three attributes are process-wide, so
    repeated calls are idempotent.
    """
    stripe.api_key = settings.STRIPE_SECRET_KEY
    stripe.api_version = settings.STRIPE_API_VERSION
    # Bound every outbound call's HTTP timeout (stripe-python's own default is ~80s).
    stripe.default_http_client = stripe.RequestsClient(timeout=settings.STRIPE_HTTP_TIMEOUT_SECONDS)
