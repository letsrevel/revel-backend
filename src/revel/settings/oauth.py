"""OAuth 2.1 / OpenID Connect *provider* (django-oauth-toolkit).

Credential presence is the feature flag (ADR-0008): the provider is enabled iff
``OIDC_SIGNING_KEY_PATH`` points at an RSA private key PEM. With it unset nothing is mounted.
See docs/superpowers/specs/2026-09-14-oauth-provider-design.md §5.
"""

from pathlib import Path

from decouple import Csv, config

OIDC_SIGNING_KEY_PATH: str = config("OIDC_SIGNING_KEY_PATH", default="")
OIDC_SIGNING_KEYS_INACTIVE_PATHS: list[str] = config("OIDC_SIGNING_KEYS_INACTIVE_PATHS", default="", cast=Csv())
# The API origin, e.g. https://api.letsrevel.io — the OIDC issuer and the RFC 9728 resource identifier.
OAUTH_ISSUER: str = config("OAUTH_ISSUER", default="")
OAUTH_MAX_APPS_PER_USER: int = config("OAUTH_MAX_APPS_PER_USER", default=10, cast=int)
OAUTH_DCR_DAILY_CAP: int = config("OAUTH_DCR_DAILY_CAP", default=500, cast=int)
OAUTH_DCR_UNUSED_TTL_HOURS: int = config("OAUTH_DCR_UNUSED_TTL_HOURS", default=24, cast=int)


def _read_pem(path: str) -> str:
    return Path(path).read_text() if path.strip() else ""


_ENABLED = bool(OIDC_SIGNING_KEY_PATH.strip())

OAUTH2_PROVIDER_APPLICATION_MODEL = "oauth.OAuthApplication"

OAUTH2_PROVIDER = {
    "OIDC_ENABLED": _ENABLED,
    "OIDC_RSA_PRIVATE_KEY": _read_pem(OIDC_SIGNING_KEY_PATH),
    "OIDC_RSA_PRIVATE_KEYS_INACTIVE": [_read_pem(p) for p in OIDC_SIGNING_KEYS_INACTIVE_PATHS],
    "OIDC_ISS_ENDPOINT": OAUTH_ISSUER,
    # OAUTH2_VALIDATOR_CLASS is added by Task 6, together with oauth/validator.py.
    "SCOPES_BACKEND_CLASS": "oauth.scopes.RegistryScopes",
    "DEFAULT_SCOPES": [],
    "PKCE_REQUIRED": True,
    "ALLOWED_REDIRECT_URI_SCHEMES": ["https", "http"],
    "ALLOW_LOCALHOST_LOOPBACK": True,
    "AUTHORIZATION_CODE_EXPIRE_SECONDS": 60,
    "ACCESS_TOKEN_EXPIRE_SECONDS": 3600,
    "ID_TOKEN_EXPIRE_SECONDS": 3600,
    "REFRESH_TOKEN_EXPIRE_SECONDS": 30 * 24 * 3600,
    "ROTATE_REFRESH_TOKEN": True,
    "REFRESH_TOKEN_REUSE_PROTECTION": True,
    "REFRESH_TOKEN_GRACE_PERIOD_SECONDS": 0,
    "COMPLIANT_BCP_RFC9700_TOKEN_STORAGE": True,
    "COMPLIANT_BCP_RFC9700_ACCESS_TOKEN_TRANSPORT": True,
    "COMPLIANT_BCP_RFC9700_IMPLICIT_GRANT": True,
    "COMPLIANT_BCP_RFC9700_PASSWORD_GRANT": True,
    "COMPLIANT_BCP_RFC9700_REFRESH_TOKEN": True,
    "COMPLIANT_BCP_RFC9700_REDIRECT_URI_MATCHING": True,
    "COMPLIANT_BCP_RFC9700_PKCE_REQUIRED": True,
    # Deploy-check gate only; it flags any "http" in the schemes list, which loopback needs.
    "COMPLIANT_BCP_RFC9700_REDIRECT_URI_SCHEME": False,
    "DCR_ENABLED": _ENABLED,
    "DCR_REGISTRATION_PERMISSION_CLASSES": ("oauth2_provider.dcr.AllowAllDCRPermission",),
    "OAUTH2_PROTECTED_RESOURCE_IDENTIFIER": OAUTH_ISSUER,
    "OAUTH2_PROTECTED_RESOURCE_AUTHORIZATION_SERVERS": [OAUTH_ISSUER] if OAUTH_ISSUER else [],
    "OIDC_RP_INITIATED_LOGOUT_ENABLED": False,
    # The five *_ADMIN_CLASS keys are added by Task 11, together with oauth/admin.py.
}
