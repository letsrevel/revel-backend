"""Thin httpx wrapper around the Eventbrite v3 API with the error mapping the service layer expects.

Rate budget (spec §7.7a): 2000/h per token *and* per app key, reported in ``x-rate-limit``.
This client records the app-key bucket's remaining calls in the cache for the reconcile to read.
"""

import re
import typing as t

import httpx
import structlog
from django.core.cache import cache

from integrations.enums import IntegrationErrorCode
from integrations.exceptions import ProviderError

logger = structlog.get_logger(__name__)

API_BASE = "https://www.eventbriteapi.com/v3"
API_HOST = "www.eventbriteapi.com"
OAUTH_AUTHORIZE = "https://www.eventbrite.com/oauth/authorize"
OAUTH_TOKEN = "https://www.eventbrite.com/oauth/token"
TIMEOUT_SECONDS = 15.0
BUDGET_CACHE_KEY = "integrations:budget:eventbrite"
BUDGET_CACHE_TTL = 3600
# The bucket refills at `reset`; caching a stale "nearly spent" reading past that point would
# stall the reconcile for a whole window, and caching it forever would never expire it.
BUDGET_CACHE_TTL_MIN = 60
_KEY_BUCKET = re.compile(r"key:\S+\s+(\d+)/(\d+)([^,]*)")
_RESET = re.compile(r"reset=(\d+)s")


def _budget_ttl(bucket: str) -> int:
    """Seconds to keep the reading: the bucket's own ``reset=<n>s``, clamped; an hour when absent."""
    reset = _RESET.search(bucket)
    if reset is None:
        return BUDGET_CACHE_TTL
    return min(max(int(reset.group(1)), BUDGET_CACHE_TTL_MIN), BUDGET_CACHE_TTL)


def _record_budget(response: httpx.Response) -> None:
    """Cache the app-key bucket's remaining calls from ``x-rate-limit`` (spec §7.7a); no-op when absent."""
    match = _KEY_BUCKET.search(response.headers.get("x-rate-limit", ""))
    if match is None:
        return
    used, limit = int(match.group(1)), int(match.group(2))
    try:
        cache.set(BUDGET_CACHE_KEY, max(limit - used, 0), _budget_ttl(match.group(3)))
    except Exception as e:
        # Fail open, like the auto-sync debounce in integrations/signals.py: the cache backend
        # raises on connection failures (see the CACHES comment in revel/settings/base.py), and
        # this bookkeeping runs *after* a successful call. Letting it raise would discard the
        # response body — losing a freshly created event's remote ID and duplicating the listing
        # on retry. A missing reading only makes the reconcile treat the budget as unknown.
        logger.warning("integration_budget_cache_unavailable", error=str(e))


def _error_message(body: dict[str, t.Any]) -> str | None:
    msg = body.get("error_description") or body.get("error")
    return str(msg) if msg else None


def _json_dict(response: httpx.Response) -> dict[str, t.Any]:
    """Parse a 2xx body as a JSON object, or raise ``ProviderError`` — never a bare ``ValueError``."""
    try:
        body = response.json()
    except ValueError as e:
        raise ProviderError(IntegrationErrorCode.PROVIDER_REJECTED, "unexpected response body") from e
    if not isinstance(body, dict):
        raise ProviderError(IntegrationErrorCode.PROVIDER_REJECTED, "unexpected response body")
    return t.cast(dict[str, t.Any], body)


def _raise_for(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    message = _error_message(body)
    if response.status_code == 401:
        raise ProviderError(IntegrationErrorCode.CONNECTION_REVOKED, message)
    if response.status_code == 429:
        raise ProviderError(IntegrationErrorCode.PROVIDER_RATE_LIMITED, message, retryable=True)
    if response.status_code == 404:
        raise ProviderError(IntegrationErrorCode.REMOTE_EVENT_MISSING, message)
    raise ProviderError(IntegrationErrorCode.PROVIDER_REJECTED, message, retryable=response.status_code >= 500)


class EventbriteClient:
    """Opens a fresh httpx client per call, by design in phase 1.

    One API call per operation; the client is opened and closed around each request rather
    than pooled across the object's lifetime. Pooling can come with the phase-3 reconcile if
    call volume justifies it.
    """

    def __init__(self, access_token: str | None = None, *, transport: httpx.BaseTransport | None = None) -> None:
        """Store the credentials for opening a client on each call; ``transport`` swaps in a fake for tests."""
        self._headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
        self._transport = transport

    def _open(self) -> httpx.Client:
        return httpx.Client(headers=self._headers, timeout=TIMEOUT_SECONDS, transport=self._transport)

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, t.Any] | None = None,
        params: dict[str, t.Any] | None = None,
    ) -> dict[str, t.Any]:
        """Call ``API_BASE + path`` and return the JSON body, mapping HTTP failures to ``ProviderError``."""
        try:
            with self._open() as http:
                response = http.request(method, f"{API_BASE}{path}", json=json, params=params)
        except httpx.HTTPError as e:
            raise ProviderError(IntegrationErrorCode.PROVIDER_REJECTED, str(e), retryable=True) from e
        _record_budget(response)
        _raise_for(response)
        return _json_dict(response)

    def exchange_code(self, client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict[str, t.Any]:
        """Form-encoded authorization-code exchange (spec §14: bare ``{access_token, token_type}``)."""
        data = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        }
        try:
            with self._open() as http:
                response = http.post(OAUTH_TOKEN, data=data)
        except httpx.HTTPError as e:
            raise ProviderError(IntegrationErrorCode.PROVIDER_REJECTED, str(e), retryable=True) from e
        _record_budget(response)
        _raise_for(response)
        return _json_dict(response)
