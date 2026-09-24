"""OAuth 2.1 / OpenID Connect *provider* (django-oauth-toolkit).

Credential presence is the feature flag (ADR-0008): the provider is enabled iff
``OIDC_SIGNING_KEY_PATH`` points at an RSA private key PEM. With it unset nothing is mounted.
See docs/superpowers/specs/2026-09-14-oauth-provider-design.md §5.
"""

from pathlib import Path

from decouple import Csv, config

OIDC_SIGNING_KEY_PATH: str = config("OIDC_SIGNING_KEY_PATH", default="")
OIDC_SIGNING_KEYS_INACTIVE_PATHS: list[str] = config("OIDC_SIGNING_KEYS_INACTIVE_PATHS", default="", cast=Csv())
# The API origin, e.g. https://api.letsrevel.io — the OIDC issuer and the RFC 9728 resource
# identifier. Normalised HERE and nowhere else: every consumer concatenates a rooted path
# onto it, so a configured trailing slash would otherwise emit "https://host//.well-known/…".
OAUTH_ISSUER: str = config("OAUTH_ISSUER", default="").rstrip("/")
OAUTH_MAX_APPS_PER_USER: int = config("OAUTH_MAX_APPS_PER_USER", default=10, cast=int)
OAUTH_DCR_DAILY_CAP: int = config("OAUTH_DCR_DAILY_CAP", default=500, cast=int)
OAUTH_DCR_UNUSED_TTL_HOURS: int = config("OAUTH_DCR_UNUSED_TTL_HOURS", default=24, cast=int)


#: Configured key paths that could not be read (missing, or not readable by this uid), as
#: ``"<path>: <reason>"``. Reported by ``oauth.checks`` as ``oauth.E002`` and consulted by
#: ``oauth.utils.oauth_provider_enabled`` — deliberately NOT raised here. A settings-import
#: crash takes every process down (web, celery, beat, telegram) for what is one optional
#: feature, and the failure is an ordinary deploy mistake: a PEM generated on the host is
#: ``0600`` to the host user while the container runs as uid 997 (the Google Wallet cert
#: precedent). The provider stays off until the file is readable.
OIDC_SIGNING_KEY_ERRORS: list[str] = []


def read_pem(path: str, errors: list[str]) -> str:
    """Read a PEM, or record why it could not be read in ``errors`` and return ``""``."""
    if not path.strip():
        return ""
    try:
        return Path(path).read_text()
    except OSError as exc:
        errors.append(f"{path}: {exc.strerror or exc}")
        return ""


_PRIVATE_KEY = read_pem(OIDC_SIGNING_KEY_PATH, OIDC_SIGNING_KEY_ERRORS)
_INACTIVE_KEYS = [read_pem(p, OIDC_SIGNING_KEY_ERRORS) for p in OIDC_SIGNING_KEYS_INACTIVE_PATHS]
_ENABLED = bool(OIDC_SIGNING_KEY_PATH.strip()) and not OIDC_SIGNING_KEY_ERRORS

OAUTH2_PROVIDER_APPLICATION_MODEL = "oauth.OAuthApplication"

OAUTH2_PROVIDER = {
    "OIDC_ENABLED": _ENABLED,
    "OIDC_RSA_PRIVATE_KEY": _PRIVATE_KEY,
    "OIDC_RSA_PRIVATE_KEYS_INACTIVE": _INACTIVE_KEYS,
    "OIDC_ISS_ENDPOINT": OAUTH_ISSUER,
    "OAUTH2_VALIDATOR_CLASS": "oauth.validator.RevelOAuth2Validator",
    # Absolutizes the userinfo URI for the RFC 8707 audience check; upstream bug #1004, removal #1005.
    "OAUTH2_BACKEND_CLASS": "oauth.backends.RevelOAuthLibCore",
    "SCOPES_BACKEND_CLASS": "oauth.scopes.RegistryScopes",
    # No SCOPES/DEFAULT_SCOPES keys here on purpose: only DOT's SettingsScopes reads them, and
    # we replace it above. The vocabulary and the (empty) defaults both live in oauth/scopes.py
    # — setting them here would be silently ignored.
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
    # Two distinct keys, and only this one refuses the "plain" code_challenge_method
    # (oauth2_validators._create_authorization_code); PKCE_REQUIRED above only requires PKCE
    # to be *present*. RFC 9700 §2.1.1 and RFC 7636 §4.2 discourage "plain" — it makes the
    # challenge its own verifier, so a leaked code is directly exchangeable — and OAuth 2.1
    # expects S256. DOT defaults it False; leaving it there is what oauth2_provider.W003
    # flags. See R-122.
    "COMPLIANT_BCP_RFC9700_PKCE_METHOD": True,
    # Deploy-check gate only; it flags any "http" in the schemes list, which loopback needs.
    "COMPLIANT_BCP_RFC9700_REDIRECT_URI_SCHEME": False,
    "DCR_ENABLED": _ENABLED,
    "DCR_REGISTRATION_PERMISSION_CLASSES": ("oauth2_provider.dcr.AllowAllDCRPermission",),
    "OAUTH2_PROTECTED_RESOURCE_IDENTIFIER": OAUTH_ISSUER,
    "OAUTH2_PROTECTED_RESOURCE_AUTHORIZATION_SERVERS": [OAUTH_ISSUER] if OAUTH_ISSUER else [],
    "OIDC_RP_INITIATED_LOGOUT_ENABLED": False,
    # DOT's own ``oauth2_provider/admin.py`` registers all five models from these import
    # strings, which it resolves during admin autodiscovery — so they must not be set before
    # ``oauth/admin.py`` exists or every ``manage.py`` invocation breaks (ADR-0018). Without
    # them the Unfold subclasses are dead code and the curated sidebar links 404;
    # ``oauth/tests/test_admin.py`` asserts the registry holds our classes, not DOT's.
    "APPLICATION_ADMIN_CLASS": "oauth.admin.OAuthApplicationAdmin",
    "ACCESS_TOKEN_ADMIN_CLASS": "oauth.admin.OAuthAccessTokenAdmin",
    "GRANT_ADMIN_CLASS": "oauth.admin.OAuthGrantAdmin",
    "ID_TOKEN_ADMIN_CLASS": "oauth.admin.OAuthIDTokenAdmin",
    "REFRESH_TOKEN_ADMIN_CLASS": "oauth.admin.OAuthRefreshTokenAdmin",
}
