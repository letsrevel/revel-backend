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


NINJA_EXTRA = {
    "THROTTLE_RATES": {
        "user": "1000/day",
        "anon": "250/day",
    },
    "NUM_PROXIES": None,
}
