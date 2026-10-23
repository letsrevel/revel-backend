"""Thin client for the Cloudflare for SaaS Custom Hostnames API (white-label custom domains, #1151).

Only the three calls the platform needs: create, get and delete. Responses are parsed into pydantic models
whose field names follow Cloudflare's published schema
(https://developers.cloudflare.com/api/resources/custom_hostnames/). Any transport failure, non-2xx status
or ``success: false`` envelope raises :class:`events.exceptions.CloudflareAPIError`.
"""

import typing as t

import httpx
import structlog
from django.conf import settings
from pydantic import BaseModel, Field

from events.exceptions import CloudflareAPIError

logger = structlog.get_logger(__name__)

CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"
REQUEST_TIMEOUT_SECONDS = 10


class OwnershipVerification(BaseModel):
    """The TXT record that proves hostname ownership (``ownership_verification``)."""

    type: str | None = None
    name: str | None = None
    value: str | None = None


class ValidationRecord(BaseModel):
    """One certificate (DCV) validation record (``ssl.validation_records[]``)."""

    status: str | None = None
    txt_name: str | None = None
    txt_value: str | None = None
    cname: str | None = None
    cname_target: str | None = None
    http_url: str | None = None
    http_body: str | None = None


class SslValidationError(BaseModel):
    """A certificate-authority validation error (``ssl.validation_errors[]``)."""

    message: str | None = None


class CustomHostnameSsl(BaseModel):
    """The ``ssl`` object of a custom hostname."""

    status: str | None = None
    method: str | None = None
    validation_records: list[ValidationRecord] = Field(default_factory=list)
    validation_errors: list[SslValidationError] = Field(default_factory=list)


class CustomHostname(BaseModel):
    """A Cloudflare custom hostname (the ``result`` of create/get).

    ``status`` is the hostname-activation status; ``ssl.status`` is the certificate status. A hostname is
    ready for traffic only when both are ``active``.
    """

    id: str
    hostname: str
    status: str
    ssl: CustomHostnameSsl | None = None
    ownership_verification: OwnershipVerification | None = None
    verification_errors: list[str] = Field(default_factory=list)


class _ApiError(BaseModel):
    code: int | None = None
    message: str | None = None


class _Envelope(BaseModel):
    success: bool = False
    errors: list[_ApiError] = Field(default_factory=list)
    result: dict[str, t.Any] | None = None


def is_configured() -> bool:
    """Return whether Cloudflare provisioning is on: both the API token and the zone id are set."""
    return bool(settings.CLOUDFLARE_API_TOKEN and settings.CLOUDFLARE_ZONE_ID)


def _request(method: str, path: str, *, json: dict[str, t.Any] | None = None) -> dict[str, t.Any] | None:
    """Call the Cloudflare API and return the envelope's ``result``.

    Raises:
        CloudflareAPIError: On a transport error, an unparseable body, a non-2xx status or ``success: false``.
    """
    url = f"{CLOUDFLARE_API_BASE}/zones/{settings.CLOUDFLARE_ZONE_ID}/custom_hostnames{path}"
    headers = {"Authorization": f"Bearer {settings.CLOUDFLARE_API_TOKEN}"}
    try:
        response = httpx.request(method, url, headers=headers, json=json, timeout=REQUEST_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        raise CloudflareAPIError(f"Cloudflare request failed: {exc.__class__.__name__}") from exc
    try:
        envelope = _Envelope.model_validate(response.json())
    except ValueError as exc:  # a non-JSON body, or pydantic's ValidationError (a ValueError subclass)
        raise CloudflareAPIError(
            f"Cloudflare returned an unparseable HTTP {response.status_code} response",
            status_code=response.status_code,
        ) from exc
    if response.is_error or not envelope.success:
        codes = tuple(e.code for e in envelope.errors if e.code is not None)
        detail = "; ".join(e.message or "" for e in envelope.errors) or "no error detail"
        logger.warning(
            "cloudflare_api_error", method=method, status_code=response.status_code, codes=codes, detail=detail
        )
        raise CloudflareAPIError(
            f"Cloudflare returned HTTP {response.status_code}: {detail}",
            status_code=response.status_code,
            error_codes=codes,
        )
    return envelope.result


def create_custom_hostname(hostname: str) -> CustomHostname:
    """Create a custom hostname with HTTP domain-control validation (the customer only needs the CNAME).

    Args:
        hostname: The normalized customer hostname.

    Returns:
        CustomHostname: The created hostname, including its id and verification records.

    Raises:
        CloudflareAPIError: If Cloudflare rejects or fails the request.
    """
    result = _request("POST", "", json={"hostname": hostname, "ssl": {"method": "http", "type": "dv"}})
    return CustomHostname.model_validate(result)


def get_custom_hostname(custom_hostname_id: str) -> CustomHostname:
    """Fetch a custom hostname's current activation and certificate status.

    Raises:
        CloudflareAPIError: If Cloudflare rejects or fails the request.
    """
    return CustomHostname.model_validate(_request("GET", f"/{custom_hostname_id}"))


def delete_custom_hostname(custom_hostname_id: str) -> None:
    """Delete a custom hostname (and its certificates).

    Raises:
        CloudflareAPIError: If Cloudflare rejects or fails the request (a 404 means it is already gone).
    """
    _request("DELETE", f"/{custom_hostname_id}")
