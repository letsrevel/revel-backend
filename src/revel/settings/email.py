from decouple import config

INTERNAL_CATCHALL_EMAIL = config("INTERNAL_CATCHALL_EMAIL", default="internal@example.com")

EMAIL_HOST = config("EMAIL_HOST", default="smtp.google.com")
EMAIL_PORT = config("EMAIL_PORT", default=587, cast=int)
EMAIL_HOST_USER = config("EMAIL_HOST_USER", default="<EMAIL>")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD", default="<PASSWORD>")
EMAIL_USE_SSL = config("EMAIL_USE_SSL", default=False, cast=bool)
EMAIL_USE_TLS = config("EMAIL_USE_TLS", default=False, cast=bool)
DEFAULT_FROM_EMAIL = config("DEFAULT_FROM_EMAIL", default="Revel <revel@letsrevel.io>")  # Project domain default

EMAIL_DRY_RUN = config("EMAIL_DRY_RUN", default=False, cast=bool)

if EMAIL_DRY_RUN:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
else:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
