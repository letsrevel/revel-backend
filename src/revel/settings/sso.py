from decouple import Csv, config

GOOGLE_SSO_ALLOWABLE_DOMAINS = ["*"]

GOOGLE_SSO_CLIENT_ID = config("GOOGLE_SSO_CLIENT_ID", "fake-id")
GOOGLE_SSO_CLIENT_SECRET = config("GOOGLE_SSO_CLIENT_SECRET", "fake-secret")
GOOGLE_SSO_PROJECT_ID = config("GOOGLE_SSO_PROJECT_ID", "fake-project-id")

GOOGLE_SSO_AUTO_CREATE_USERS = True
GOOGLE_SSO_ALWAYS_UPDATE_USER_DATA = True

# SSO_SHOW_FORM_ON_ADMIN_PAGE = DEBUG

GOOGLE_SSO_SUPERUSER_LIST = [
    _email
    for _email in config("GOOGLE_SSO_SUPERUSER_LIST", cast=Csv(), default="me@kbiagiodistefano.io,")
    if "@" in _email
]
_GOOGLE_SSO_STAFF_LIST = [_email for _email in config("GOOGLE_SSO_STAFF_LIST", cast=Csv(), default="") if "@" in _email]
GOOGLE_SSO_STAFF_LIST = GOOGLE_SSO_SUPERUSER_LIST + _GOOGLE_SSO_STAFF_LIST

# more settings: https://megalus.github.io/django-google-sso/settings/
