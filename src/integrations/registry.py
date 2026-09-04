"""Provider registry. A provider is registered only when its credentials are configured."""

from django.conf import settings
from django.utils.translation import gettext_lazy as _

from integrations.exceptions import IntegrationError
from integrations.providers.base import ListingProvider
from integrations.schema import IntegrationErrorCode

PROVIDERS: dict[str, ListingProvider] = {}


def populate() -> None:
    """(Re)build ``PROVIDERS`` from settings. Called from ``IntegrationsConfig.ready`` and tests."""
    from integrations.providers.eventbrite.provider import EventbriteProvider
    from revel.settings.integrations import eventbrite_enabled

    PROVIDERS.clear()
    if eventbrite_enabled(settings.INTEGRATIONS_EVENTBRITE_CLIENT_ID, settings.INTEGRATIONS_EVENTBRITE_CLIENT_SECRET):
        PROVIDERS[EventbriteProvider.key] = EventbriteProvider(
            client_id=settings.INTEGRATIONS_EVENTBRITE_CLIENT_ID,
            client_secret=settings.INTEGRATIONS_EVENTBRITE_CLIENT_SECRET,
        )


def enabled_providers() -> list[ListingProvider]:
    """Providers an organization may connect, in registration order."""
    return list(PROVIDERS.values())


def get_provider(key: str) -> ListingProvider:
    """Look up an enabled provider or raise a 404-mapped ``IntegrationError``."""
    try:
        return PROVIDERS[key]
    except KeyError:
        raise IntegrationError(
            IntegrationErrorCode.PROVIDER_UNKNOWN, str(_("Unknown or disabled provider.")), status=404
        ) from None
