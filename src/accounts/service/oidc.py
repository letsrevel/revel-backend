"""Generic OpenID Connect relying-party flow.

Backend-handled authorization-code + PKCE login against any provider in
``settings.OIDC_PROVIDERS``. See docs/superpowers/specs/2026-09-02-oidc-relying-party-design.md
and ADR-0016.
"""

import base64
import hashlib
import secrets
import typing as t
from urllib.parse import urlencode

import httpx
import structlog
from django.conf import settings
from django.core.cache import cache
from django.http import Http404
from django.utils.http import url_has_allowed_host_and_scheme

from accounts.exceptions import OIDCLoginError
from revel.oidc_config import OIDCProviderConfig

logger = structlog.get_logger(__name__)

STATE_TTL_SECONDS = 600
DISCOVERY_TTL_SECONDS = 24 * 3600
HTTP_TIMEOUT_SECONDS = 10.0
ALLOWED_ID_TOKEN_ALGS = ["RS256", "ES256"]


def get_provider(key: str) -> OIDCProviderConfig:
    """Return the configured provider for ``key`` or raise 404."""
    for provider in settings.OIDC_PROVIDERS:
        if provider.key == key:
            return t.cast(OIDCProviderConfig, provider)
    raise Http404("Unknown OIDC provider.")


def list_providers() -> list[OIDCProviderConfig]:
    """All configured providers, in configuration order."""
    return list(settings.OIDC_PROVIDERS)


def safe_return_url(url: str | None) -> str:
    """Accept only a relative path (``/...``), never a scheme or host. Defaults to ``/``.

    Host/scheme rejection (including control-character smuggling via a tab, CR, or LF right
    after the leading slash, which ``urlsplit`` would otherwise resolve to an external host)
    is delegated to Django's own :func:`~django.utils.http.url_has_allowed_host_and_scheme`.
    """
    if (
        not url
        or not url.startswith("/")
        or url.startswith("//")
        or url.startswith("/\\")
        or not url_has_allowed_host_and_scheme(url, allowed_hosts=None)
    ):
        return "/"
    return url


def _http_client() -> httpx.Client:
    """Factory for the outbound HTTP client (monkeypatched in tests)."""
    return httpx.Client(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False)


def _state_key(state: str) -> str:
    return f"oidc:state:{state}"


def _discovery_key(provider: OIDCProviderConfig) -> str:
    return f"oidc:discovery:{provider.key}"


def discovery(provider: OIDCProviderConfig) -> dict[str, t.Any]:
    """Fetch (and cache for a day) the provider's OpenID configuration document.

    Raises:
        OIDCLoginError("provider"): On transport failure, non-2xx, or issuer mismatch.
    """
    cached = cache.get(_discovery_key(provider))
    if cached is not None:
        return t.cast(dict[str, t.Any], cached)
    url = f"{provider.issuer}/.well-known/openid-configuration"
    try:
        with _http_client() as client:
            response = client.get(url)
            response.raise_for_status()
            doc = t.cast(dict[str, t.Any], response.json())
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as e:
        logger.warning("oidc_discovery_failed", provider=provider.key, error=str(e))
        raise OIDCLoginError("provider") from e
    if doc.get("issuer", "").rstrip("/") != provider.issuer:
        logger.warning("oidc_discovery_issuer_mismatch", provider=provider.key, issuer=doc.get("issuer"))
        raise OIDCLoginError("provider")
    for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not doc.get(field):
            logger.warning("oidc_discovery_missing_field", provider=provider.key, field=field)
            raise OIDCLoginError("provider")
    cache.set(_discovery_key(provider), doc, DISCOVERY_TTL_SECONDS)
    return doc


def _redirect_uri(provider: OIDCProviderConfig) -> str:
    return f"{settings.BASE_URL}/api/auth/oidc/{provider.key}/callback"


def begin_login(provider: OIDCProviderConfig, return_url: str | None) -> str:
    """Start a login: store state/nonce/PKCE in the cache and return the IdP authorization URL."""
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    cache.set(
        _state_key(state),
        {"provider": provider.key, "nonce": nonce, "verifier": verifier, "return_url": safe_return_url(return_url)},
        STATE_TTL_SECONDS,
    )
    params = {
        "response_type": "code",
        "client_id": provider.client_id,
        "redirect_uri": _redirect_uri(provider),
        "scope": provider.scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    logger.info("oidc_login_started", provider=provider.key)
    return f"{discovery(provider)['authorization_endpoint']}?{urlencode(params)}"
