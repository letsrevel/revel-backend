"""External listing platforms (Eventbrite first).

See docs/superpowers/specs/2026-09-04-platform-listings-design.md §4. A provider is
offered to organizations only when both of its credentials are set — credential presence
is the feature flag, same as the OIDC providers in ``revel.settings.sso``.
"""

from datetime import timedelta

from decouple import config


def eventbrite_enabled(client_id: str, client_secret: str) -> bool:
    """Whether the Eventbrite provider can be offered (both app credentials present)."""
    return bool(client_id.strip()) and bool(client_secret.strip())


INTEGRATIONS_EVENTBRITE_CLIENT_ID: str = config("INTEGRATIONS_EVENTBRITE_CLIENT_ID", default="")
INTEGRATIONS_EVENTBRITE_CLIENT_SECRET: str = config("INTEGRATIONS_EVENTBRITE_CLIENT_SECRET", default="")
# Lifetime of the signed state minted by "Connect": long enough for the provider's consent
# screen, short enough that a leaked URL is worthless by the time anyone finds it.
INTEGRATIONS_CONNECT_STATE_TTL = timedelta(
    seconds=config("INTEGRATIONS_CONNECT_STATE_TTL_SECONDS", default=600, cast=int)
)
