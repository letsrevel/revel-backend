from datetime import timedelta

from decouple import config

from .base import SECRET_KEY

JWT_ALGORITHM = config("JWT_ALGORITHM", default="HS256")
JWT_AUDIENCE = config("JWT_AUDIENCE", default="letsrevel.io")

NINJA_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=config("ACCESS_TOKEN_LIFETIME_HOURS", default=1, cast=int)),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=config("REFRESH_TOKEN_LIFETIME_DAYS", default=30, cast=int)),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "ALGORITHM": JWT_ALGORITHM,
    "SIGNING_KEY": SECRET_KEY,
    "AUDIENCE": JWT_AUDIENCE,
}
VERIFY_TOKEN_LIFETIME = timedelta(minutes=config("VERIFY_TOKEN_LIFETIME_MINUTES", default=15, cast=int))
UNSUBSCRIBE_TOKEN_LIFETIME = timedelta(days=config("UNSUBSCRIBE_TOKEN_LIFETIME_DAYS", default=30, cast=int))

# Impersonation settings
IMPERSONATION_REQUEST_TOKEN_LIFETIME = timedelta(
    seconds=config("IMPERSONATION_REQUEST_TOKEN_LIFETIME_SECONDS", default=120, cast=int)
)
IMPERSONATION_ACCESS_TOKEN_LIFETIME = timedelta(
    minutes=config("IMPERSONATION_ACCESS_TOKEN_LIFETIME_MINUTES", default=15, cast=int)
)


NINJA_EXTRA = {
    "THROTTLE_RATES": {
        "user": "1000/day",
        "anon": "250/day",
    },
    # Exactly one trusted reverse-proxy hop (Caddy) sits in front of the app in
    # production. With NUM_PROXIES=1, ninja_extra's throttle get_ident() trusts only
    # the last X-Forwarded-For entry (the IP Caddy appends) and discards any
    # client-forged prefix, so anonymous throttle buckets cannot be evaded by sending
    # a random X-Forwarded-For header. NUM_PROXIES=None would key throttling on the
    # full client-controlled header, defeating brute-force/DoS protection.
    "NUM_PROXIES": config("NUM_PROXIES", default=1, cast=int),
}
