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
import jwt
import structlog
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.utils.http import url_has_allowed_host_and_scheme
from pydantic import BaseModel, EmailStr, ValidationError

from accounts.exceptions import OIDCLoginError
from accounts.models import ExternalIdentity, RevelUser
from common.utils import get_or_create_with_race_protection
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


class OIDCClaims(BaseModel):
    """The subset of ID-token claims Revel uses."""

    sub: str
    email: EmailStr | None = None
    email_verified: bool = False
    given_name: str = ""
    family_name: str = ""
    locale: str | None = None
    picture: str | None = None


_jwk_clients: dict[tuple[str, str], jwt.PyJWKClient] = {}


def _signing_key(provider: OIDCProviderConfig, id_token: str) -> t.Any:
    """Resolve the ID token's signing key from the provider's JWKS (cached per process).

    Cached by ``(provider.key, jwks_uri)`` so a JWKS URI change (once ``discovery()``'s cache
    expires and re-fetches a new one) rebuilds the client instead of pinning the old endpoint
    forever. Monkeypatched in tests to return a local public key.
    """
    jwks_uri = discovery(provider)["jwks_uri"]
    cache_key = (provider.key, jwks_uri)
    client = _jwk_clients.get(cache_key)
    if client is None:
        client = jwt.PyJWKClient(jwks_uri, cache_keys=True, lifespan=3600)
        _jwk_clients[cache_key] = client
    return client.get_signing_key_from_jwt(id_token).key


def _pop_state(provider: OIDCProviderConfig, state: str) -> dict[str, t.Any]:
    """Consume the state entry created by :func:`begin_login`. Single use."""
    key = _state_key(state)
    entry = cache.get(key)
    cache.delete(key)
    if not entry or entry.get("provider") != provider.key:
        logger.warning("oidc_state_invalid", provider=provider.key)
        raise OIDCLoginError("state")
    return t.cast(dict[str, t.Any], entry)


def _exchange_code(provider: OIDCProviderConfig, code: str, verifier: str) -> str:
    """Redeem the authorization code at the token endpoint and return the raw ``id_token``."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _redirect_uri(provider),
        "client_id": provider.client_id,
        "client_secret": provider.client_secret,
        "code_verifier": verifier,
    }
    try:
        with _http_client() as client:
            response = client.post(discovery(provider)["token_endpoint"], data=data)
            response.raise_for_status()
            body = t.cast(dict[str, t.Any], response.json())
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as e:
        logger.warning("oidc_token_exchange_failed", provider=provider.key, error=str(e))
        raise OIDCLoginError("provider") from e
    id_token = body.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        logger.warning("oidc_token_response_missing_id_token", provider=provider.key)
        raise OIDCLoginError("provider")
    return id_token


def _verify_id_token(provider: OIDCProviderConfig, id_token: str, nonce: str) -> OIDCClaims:
    """Verify signature, issuer, audience, expiry and nonce; return the parsed claims."""
    try:
        payload = jwt.decode(
            id_token,
            key=_signing_key(provider, id_token),
            algorithms=ALLOWED_ID_TOKEN_ALGS,
            audience=provider.client_id,
            issuer=discovery(provider)["issuer"],
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as e:
        logger.warning("oidc_id_token_invalid", provider=provider.key, error=str(e))
        raise OIDCLoginError("provider") from e
    if payload.get("nonce") != nonce:
        logger.warning("oidc_nonce_mismatch", provider=provider.key)
        raise OIDCLoginError("state")
    if "azp" in payload and payload["azp"] != provider.client_id:
        logger.warning("oidc_azp_mismatch", provider=provider.key)
        raise OIDCLoginError("provider")
    try:
        return OIDCClaims.model_validate(payload)
    except ValidationError as e:
        logger.warning("oidc_id_token_claims_invalid", provider=provider.key)
        raise OIDCLoginError("provider") from e


def _language_from_locale(locale: str | None) -> str:
    """Map an IdP locale like ``de-AT`` to a supported language code, else the site default."""
    if not locale:
        return str(settings.LANGUAGE_CODE)
    lang = locale.split("-")[0].lower()
    return lang if lang in {code for code, _ in settings.LANGUAGES} else str(settings.LANGUAGE_CODE)


@transaction.atomic
def _resolve_user(provider: OIDCProviderConfig, claims: OIDCClaims) -> RevelUser:  # noqa: C901
    """Find the user for verified claims, linking or creating as needed.

    Order: existing identity → existing account by email (link, only if the IdP
    asserts ``email_verified``) → new account. ``is_staff``/``is_superuser`` are never
    derived from claims.
    """
    from accounts.service.global_ban_service import is_email_globally_banned

    identity = ExternalIdentity.objects.select_related("user").filter(provider=provider.key, subject=claims.sub).first()
    if identity is not None:
        identity_user = identity.user
        if is_email_globally_banned(identity_user.email):
            raise OIDCLoginError("banned")
        if not identity_user.is_active:
            raise OIDCLoginError("inactive")
        logger.info(
            "oidc_login_completed", provider=provider.key, user_id=str(identity_user.id), created=False, linked=False
        )
        return identity_user

    if not claims.email:
        raise OIDCLoginError("no_email")
    email = str(claims.email)
    if is_email_globally_banned(email):
        raise OIDCLoginError("banned")

    user = RevelUser.objects.select_for_update().filter(username__iexact=email).first()
    created = False
    if user is not None:
        if not claims.email_verified:
            logger.warning("oidc_link_refused_unverified_email", provider=provider.key, user_id=str(user.id))
            raise OIDCLoginError("unverified_email")
        if not user.is_active and not user.guest:
            raise OIDCLoginError("inactive")
        if user.guest:
            user.guest = False
            user.email_verified = True
            user.is_active = True
            user.save(update_fields=["guest", "email_verified", "is_active"])
        elif not user.email_verified:
            user.email_verified = True
            user.save(update_fields=["email_verified"])
    else:
        user, created = get_or_create_with_race_protection(
            RevelUser,
            Q(username__iexact=email),
            {
                "username": email,
                "email": email,
                "first_name": claims.given_name,
                "last_name": claims.family_name,
                "email_verified": True,
                "guest": False,
                "is_active": True,
                "language": _language_from_locale(claims.locale),
            },
        )
        if created:
            user.set_unusable_password()
            user.save(update_fields=["password"])

    identity, _ = get_or_create_with_race_protection(
        ExternalIdentity,
        Q(provider=provider.key, subject=claims.sub),
        {"user": user, "provider": provider.key, "subject": claims.sub, "email": email},
    )
    logger.info("oidc_login_completed", provider=provider.key, user_id=str(user.id), created=created, linked=True)
    return user
