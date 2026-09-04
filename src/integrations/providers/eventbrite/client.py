"""Thin httpx wrapper around the Eventbrite v3 API with the error mapping the service layer expects.

Rate budget (spec §7.7a): 2000/h per token *and* per app key, reported in ``x-rate-limit``.
Phase 1 only surfaces 429 as retryable; the budget-aware reconcile arrives with phase 3.
"""

import typing as t

import httpx

from integrations.exceptions import ProviderError
from integrations.schema import IntegrationErrorCode

API_BASE = "https://www.eventbriteapi.com/v3"
API_HOST = "www.eventbriteapi.com"
OAUTH_AUTHORIZE = "https://www.eventbrite.com/oauth/authorize"
OAUTH_TOKEN = "https://www.eventbrite.com/oauth/token"
TIMEOUT_SECONDS = 15.0


def _error_message(body: dict[str, t.Any]) -> str | None:
    msg = body.get("error_description") or body.get("error")
    return str(msg) if msg else None


def _raise_for(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    try:
        body = t.cast(dict[str, t.Any], response.json())
    except ValueError:
        body = {}
    message = _error_message(body)
    if response.status_code == 401:
        raise ProviderError(IntegrationErrorCode.CONNECTION_REVOKED, message)
    if response.status_code == 429:
        raise ProviderError(IntegrationErrorCode.PROVIDER_RATE_LIMITED, message, retryable=True)
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
        _raise_for(response)
        return t.cast(dict[str, t.Any], response.json())

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
        _raise_for(response)
        return t.cast(dict[str, t.Any], response.json())
