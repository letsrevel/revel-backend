from datetime import timedelta

from decouple import Csv, config

from revel.oidc_config import load_oidc_providers

from .base import DEBUG

GOOGLE_SSO_ALLOWABLE_DOMAINS = ["*"]

GOOGLE_SSO_CLIENT_ID = config("GOOGLE_SSO_CLIENT_ID", "fake-id")
GOOGLE_SSO_CLIENT_SECRET = config("GOOGLE_SSO_CLIENT_SECRET", "fake-secret")
GOOGLE_SSO_PROJECT_ID = config("GOOGLE_SSO_PROJECT_ID", "fake-project-id")

GOOGLE_SSO_AUTO_CREATE_USERS = True
GOOGLE_SSO_ALWAYS_UPDATE_USER_DATA = True

GOOGLE_SSO_SUPERUSER_LIST = [
    _email
    for _email in config("GOOGLE_SSO_SUPERUSER_LIST", cast=Csv(), default="me@kbiagiodistefano.io,")
    if "@" in _email
]
_GOOGLE_SSO_STAFF_LIST = [_email for _email in config("GOOGLE_SSO_STAFF_LIST", cast=Csv(), default="") if "@" in _email]
GOOGLE_SSO_STAFF_LIST = GOOGLE_SSO_SUPERUSER_LIST + _GOOGLE_SSO_STAFF_LIST

SSO_SHOW_FORM_ON_ADMIN_PAGE = config("SSO_SHOW_FORM_ON_ADMIN_PAGE", cast=bool, default=True)

# more settings: https://megalus.github.io/django-google-sso/settings/

# Generic OpenID Connect login (see docs/superpowers/specs/2026-09-02-oidc-relying-party-design.md
# and ADR-0016). Empty tuple = SSO off. The django-google-sso settings above remain for the
# Django admin login page only.
OIDC_PROVIDERS = load_oidc_providers(config, debug=DEBUG)
OIDC_LOGIN_TOKEN_LIFETIME = timedelta(seconds=config("OIDC_LOGIN_TOKEN_LIFETIME_SECONDS", default=60, cast=int))
