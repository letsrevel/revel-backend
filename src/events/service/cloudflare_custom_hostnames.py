"""Thin client for the Cloudflare for SaaS Custom Hostnames API (white-label custom domains, #1151).

Only the three calls the platform needs: create, get and delete. Responses are parsed into pydantic models
whose field names follow Cloudflare's published schema
(https://developers.cloudflare.com/api/resources/custom_hostnames/). Any failure raises
:class:`events.exceptions.CloudflareAPIError` (and is logged once, without secrets): a transport error, a
non-2xx status, a ``success: false`` envelope, a body that is not JSON, or a missing / malformed ``result``.

It also hosts :func:`dns_query`, the public-DNS lookup (DNS-over-HTTPS) shared with the pre-flight diagnostics
(:mod:`events.service.dns_diagnostics`), and :func:`txt_record_published`, the public-DNS check that gates
activation (the ownership TXT record).
"""

import re
import typing as t

import httpx
import structlog
from django.conf import settings
from pydantic import BaseModel, Field, ValidationError

from events.exceptions import CloudflareAPIError

logger = structlog.get_logger(__name__)

CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"
REQUEST_TIMEOUT_SECONDS = 10
# Public DNS-over-HTTPS resolver (JSON API) used to check the ownership TXT record and diagnose DNS mistakes.
DNS_OVER_HTTPS_URL = "https://cloudflare-dns.com/dns-query"
DNS_LOOKUP_TIMEOUT_SECONDS = 5
DnsRecordType = t.Literal["A", "AAAA", "CAA", "CNAME", "NS", "TXT"]
_DNS_TYPE_CODES: dict[DnsRecordType, int] = {"A": 1, "NS": 2, "CNAME": 5, "TXT": 16, "AAAA": 28, "CAA": 257}
_DNS_NOERROR, _DNS_NXDOMAIN = 0, 3
_QUOTED_TXT_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')


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


def _api_error(
    message: str,
    *,
    cause: str,
    method: str,
    status_code: int | None = None,
    error_codes: tuple[int, ...] = (),
    detail: str | None = None,
    hostname_id: str | None = None,
) -> CloudflareAPIError:
    """Log a failed Cloudflare call once and build the exception for the caller to raise.

    Every ``CloudflareAPIError`` this module raises goes through here, so each failure is logged exactly
    once. Only the method, status, error codes and Cloudflare's own error text are logged; the request
    headers (the API token) and body are not.
    """
    logger.warning(
        "cloudflare_api_error",
        cause=cause,
        method=method,
        status_code=status_code,
        codes=error_codes,
        detail=detail,
    )
    return CloudflareAPIError(message, status_code=status_code, error_codes=error_codes, hostname_id=hostname_id)


def request_result(method: str, url: str, *, json: dict[str, t.Any] | None = None) -> dict[str, t.Any] | None:
    """Call a Cloudflare API URL with the platform token and return the envelope's ``result``.

    Shared by the custom-hostnames calls here and the Turnstile widget client
    (:mod:`events.service.cloudflare_turnstile`), so both fail and log the same way.

    Raises:
        CloudflareAPIError: On a transport error, an unparseable body, a non-2xx status or ``success: false``.
    """
    headers = {"Authorization": f"Bearer {settings.CLOUDFLARE_API_TOKEN}"}
    try:
        response = httpx.request(method, url, headers=headers, json=json, timeout=REQUEST_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        name = exc.__class__.__name__
        raise _api_error(f"Cloudflare request failed: {name}", cause="transport", method=method, detail=name) from exc
    try:
        envelope = _Envelope.model_validate(response.json())
    except ValueError as exc:  # a non-JSON body, or pydantic's ValidationError (a ValueError subclass)
        raise _api_error(
            f"Cloudflare returned an unparseable HTTP {response.status_code} response",
            cause="unparseable_body",
            method=method,
            status_code=response.status_code,
        ) from exc
    if response.is_error or not envelope.success:
        codes = tuple(e.code for e in envelope.errors if e.code is not None)
        detail = "; ".join(e.message or "" for e in envelope.errors) or "no error detail"
        raise _api_error(
            f"Cloudflare returned HTTP {response.status_code}: {detail}",
            cause="error_response",
            method=method,
            status_code=response.status_code,
            error_codes=codes,
            detail=detail,
        )
    return envelope.result


def _request(method: str, path: str, *, json: dict[str, t.Any] | None = None) -> dict[str, t.Any] | None:
    """Call the zone's custom-hostnames API; see :func:`request_result`."""
    url = f"{CLOUDFLARE_API_BASE}/zones/{settings.CLOUDFLARE_ZONE_ID}/custom_hostnames{path}"
    return request_result(method, url, json=json)


def _parse_hostname(method: str, result: dict[str, t.Any] | None) -> CustomHostname:
    """Parse an envelope ``result`` into a :class:`CustomHostname`.

    Raises:
        CloudflareAPIError: The result is missing or does not have the expected shape. ``hostname_id``
            carries the raw ``id`` when there is one, so a caller that just created the hostname can
            delete it again.
    """
    if result is None:
        raise _api_error("Cloudflare returned no result", cause="missing_result", method=method)
    try:
        return CustomHostname.model_validate(result)
    except ValidationError as exc:
        raw_id = result.get("id")
        raise _api_error(
            "Cloudflare returned a result with an unexpected shape",
            cause="bad_result_shape",
            method=method,
            detail=f"{exc.error_count()} validation error(s)",
            hostname_id=raw_id if isinstance(raw_id, str) and raw_id else None,
        ) from exc


def create_custom_hostname(hostname: str) -> CustomHostname:
    """Create a custom hostname with HTTP domain-control validation (the customer only needs the CNAME).

    Args:
        hostname: The normalized customer hostname.

    Returns:
        CustomHostname: The created hostname, including its id and verification records.

    Raises:
        CloudflareAPIError: If Cloudflare rejects or fails the request, or the result cannot be parsed
            (then ``hostname_id`` carries the id of the hostname that was created, when known).
    """
    result = _request("POST", "", json={"hostname": hostname, "ssl": {"method": "http", "type": "dv"}})
    return _parse_hostname("POST", result)


def get_custom_hostname(custom_hostname_id: str) -> CustomHostname:
    """Fetch a custom hostname's current activation and certificate status.

    Raises:
        CloudflareAPIError: If Cloudflare rejects or fails the request, or the result cannot be parsed.
    """
    return _parse_hostname("GET", _request("GET", f"/{custom_hostname_id}"))


def delete_custom_hostname(custom_hostname_id: str) -> None:
    """Delete a custom hostname (and its certificates).

    Raises:
        CloudflareAPIError: If Cloudflare rejects or fails the request (a 404 means it is already gone).
    """
    _request("DELETE", f"/{custom_hostname_id}")


def dns_query(name: str, rtype: DnsRecordType) -> list[str] | None:
    """Look up the ``rtype`` records of ``name`` in public DNS (over DNS-over-HTTPS).

    Returns:
        list[str] | None: The ``data`` of every answer of the requested type, in the resolver's presentation
        format (a CNAME or NS target keeps its trailing dot, a TXT value its quotes, a CAA record reads
        ``0 issue "letsencrypt.org"``). Empty when the name has no such record (NOERROR without an answer, or
        NXDOMAIN). ``None`` when the lookup failed (transport error, non-200, SERVFAIL, malformed JSON), so
        callers can tell "no record" from "could not look". Never raises; a failure is logged.
    """
    try:
        response = httpx.get(
            DNS_OVER_HTTPS_URL,
            params={"name": name, "type": rtype},
            headers={"accept": "application/dns-json"},
            timeout=DNS_LOOKUP_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
        if response.status_code != 200:
            logger.warning("custom_domain_dns_lookup_failed", name=name, rtype=rtype, status_code=response.status_code)
            return None
        body = response.json()
        if body.get("Status") not in (_DNS_NOERROR, _DNS_NXDOMAIN):
            logger.warning("custom_domain_dns_lookup_failed", name=name, rtype=rtype, dns_status=body.get("Status"))
            return None
        return [answer["data"] for answer in body.get("Answer") or [] if answer.get("type") == _DNS_TYPE_CODES[rtype]]
    except (httpx.HTTPError, ValueError, AttributeError, KeyError, TypeError) as exc:
        logger.warning("custom_domain_dns_lookup_failed", name=name, rtype=rtype, detail=exc.__class__.__name__)
        return None


def txt_record_published(name: str, value: str) -> bool:
    """Return whether public DNS serves a TXT record ``name`` whose value is exactly ``value``.

    Cloudflare activates a custom hostname on the CNAME alone, without the ownership TXT record, so the
    platform checks the TXT itself (see :func:`dns_query`) before activating a domain. Fails closed: any
    resolver problem is logged and answers ``False``; it never raises. A TXT record split into several
    quoted strings is joined before comparing.
    """
    return any(value == "".join(_QUOTED_TXT_STRING.findall(data)) for data in dns_query(name, "TXT") or [])
