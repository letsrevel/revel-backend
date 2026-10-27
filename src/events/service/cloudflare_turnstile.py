"""Thin client for the Cloudflare Turnstile widget API (hostname allowlist of the registration widget, #1148).

Two calls: fetch the widget and update it. The update is a PUT that needs the widget's whole configuration
(``name``, ``mode`` and ``domains`` are required), so :func:`set_widget_domains` writes back everything the fetch
returned, changing only ``domains``. Endpoint (``GET`` / ``PUT``
``/accounts/{account_id}/challenges/widgets/{sitekey}``):
https://developers.cloudflare.com/api/resources/turnstile/subresources/widgets/. Cloudflare documents that the
widget works on the listed hostnames "and their subdomains".
Failures raise :class:`events.exceptions.CloudflareAPIError`, logged once, like the custom-hostnames client.
"""

import typing as t

import structlog
from django.conf import settings
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from events.exceptions import CloudflareAPIError
from events.service.cloudflare_custom_hostnames import CLOUDFLARE_API_BASE, request_result

logger = structlog.get_logger(__name__)

# Server-managed fields: returned by the fetch, not accepted back by the update.
_READ_ONLY_FIELDS = frozenset({"sitekey", "secret", "created_on", "modified_on", "deployed_via", "last_modified_via"})


class TurnstileWidget(BaseModel):
    """A Turnstile widget's configuration (the ``result`` of the fetch).

    Fields Revel does not model are kept (``extra="allow"``) so the update writes them back unchanged.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    mode: str
    domains: list[str] = Field(default_factory=list)


def is_configured() -> bool:
    """Return whether the Turnstile hostname automation is on: token, account id and widget site key are all set."""
    return bool(settings.CLOUDFLARE_API_TOKEN and settings.CLOUDFLARE_ACCOUNT_ID and settings.TURNSTILE_SITE_KEY)


def _widget_url() -> str:
    account, sitekey = settings.CLOUDFLARE_ACCOUNT_ID, settings.TURNSTILE_SITE_KEY
    return f"{CLOUDFLARE_API_BASE}/accounts/{account}/challenges/widgets/{sitekey}"


def _parse_widget(method: str, result: dict[str, t.Any] | None) -> TurnstileWidget:
    if result is None:
        raise CloudflareAPIError("Cloudflare returned no widget")
    try:
        return TurnstileWidget.model_validate(result)
    except ValidationError as exc:
        logger.warning("cloudflare_api_error", cause="bad_result_shape", method=method, detail=exc.error_count())
        raise CloudflareAPIError("Cloudflare returned a widget with an unexpected shape") from exc


def get_widget() -> TurnstileWidget:
    """Fetch the registration widget (the one ``TURNSTILE_SITE_KEY`` identifies).

    Raises:
        CloudflareAPIError: If Cloudflare rejects or fails the request, or the widget cannot be parsed.
    """
    return _parse_widget("GET", request_result("GET", _widget_url()))


def set_widget_domains(widget: TurnstileWidget, domains: list[str]) -> TurnstileWidget:
    """Replace the widget's hostname list, preserving every other field of ``widget``.

    Args:
        widget: The widget as last fetched (read-modify-write).
        domains: The complete new hostname list.

    Raises:
        CloudflareAPIError: If Cloudflare rejects or fails the request, or the widget cannot be parsed.
    """
    body = {k: v for k, v in widget.model_dump().items() if k not in _READ_ONLY_FIELDS}
    body["domains"] = domains
    return _parse_widget("PUT", request_result("PUT", _widget_url(), json=body))
