"""Inbound webhook intake. Phase 1: authenticate by path secret, parse, record. Phase 3 dispatches."""

from django.http import Http404, HttpRequest
from django.utils.translation import gettext_lazy as _

from integrations import registry
from integrations.exceptions import IntegrationError, ProviderError
from integrations.models import PlatformConnection, WebhookDelivery


def record_delivery(provider_key: str, secret: str, request: HttpRequest) -> WebhookDelivery:
    """Resolve the connection by secret, parse the body via the provider, and persist the audit row."""
    provider = registry.get_provider(provider_key)
    conn = PlatformConnection.objects.filter(provider=provider_key, webhook_secret=secret).first()
    if conn is None:
        raise Http404
    try:
        notification = provider.parse_webhook(request)
    except ProviderError as e:
        raise IntegrationError(e.code, str(_("Malformed webhook delivery.")), e.provider_message) from e
    return WebhookDelivery.objects.create(
        connection=conn, action=notification.action, resource_path=notification.resource_path, payload=notification.raw
    )
