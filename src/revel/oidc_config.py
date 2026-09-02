"""Env-backed OpenID Connect provider configuration.

Pure Python on purpose: it is imported from ``revel.settings.sso`` at settings-load
time, before Django apps are ready, so it must not import Django models or apps.

# ponytail: env-only provider config. Ceiling: no runtime changes, no per-organization
# providers. Upgrade path: an ``OIDCProvider`` model with an encrypted secret that yields
# the same ``OIDCProviderConfig`` dataclass, so the service layer does not change.
"""

import re
import typing as t
from dataclasses import dataclass

from django.core.exceptions import ImproperlyConfigured

_KEY_RE = re.compile(r"^[a-z0-9_-]+$")
DEFAULT_SCOPES = "openid email profile"


@dataclass(frozen=True)
class OIDCProviderConfig:
    """One configured OpenID Connect issuer."""

    key: str
    name: str
    issuer: str
    client_id: str
    client_secret: str
    scopes: str = DEFAULT_SCOPES


def load_oidc_providers(get: t.Callable[..., t.Any], *, debug: bool) -> tuple[OIDCProviderConfig, ...]:
    """Parse ``OIDC_PROVIDERS`` and the per-provider ``OIDC_<KEY>_*`` variables.

    Args:
        get: A ``decouple.config``-compatible getter: ``get(name, default=...)``.
        debug: When True, ``http://`` issuers are allowed (local Keycloak).

    Returns:
        A tuple of providers in the order listed in ``OIDC_PROVIDERS``.

    Raises:
        ImproperlyConfigured: On a bad key, a missing required variable, or a
            non-https issuer outside debug.
    """
    raw = str(get("OIDC_PROVIDERS", default="") or "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    providers: list[OIDCProviderConfig] = []
    for key in keys:
        if not _KEY_RE.match(key):
            raise ImproperlyConfigured(f"OIDC provider key {key!r} must match {_KEY_RE.pattern}")
        prefix = f"OIDC_{key.upper().replace('-', '_')}_"
        required: dict[str, str] = {}
        for suffix in ("ISSUER", "CLIENT_ID", "CLIENT_SECRET"):
            value = str(get(prefix + suffix, default="") or "").strip()
            if not value:
                raise ImproperlyConfigured(f"{prefix + suffix} is required for OIDC provider {key!r}")
            required[suffix] = value
        issuer = required["ISSUER"].rstrip("/")
        if not issuer.startswith("https://") and not debug:
            raise ImproperlyConfigured(f"{prefix}ISSUER must use https (got {issuer!r})")
        providers.append(
            OIDCProviderConfig(
                key=key,
                name=str(get(prefix + "NAME", default="") or key.title()),
                issuer=issuer,
                client_id=required["CLIENT_ID"],
                client_secret=required["CLIENT_SECRET"],
                scopes=str(get(prefix + "SCOPES", default=DEFAULT_SCOPES) or DEFAULT_SCOPES),
            )
        )
    return tuple(providers)
