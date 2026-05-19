"""Hardcoded reserved slug tokens for organizations.

These tokens are platform-critical and must never be deletable through Django
admin. Soft additions live in the ``ReservedSlugToken`` DB table and are
unioned with this set at lookup time.

Tokens are matched against the slugified organization name split on ``-``.
A name slugifies to one or more tokens; if any token is in the reserved set,
creation is rejected at the service layer.
"""

RESERVED_SLUG_TOKENS_HARDCODED: frozenset[str] = frozenset(
    {
        # Platform / auth / web infrastructure.
        "admin",
        "api",
        "www",
        "revel",
        "letsrevel",
        "auth",
        "login",
        "logout",
        "signup",
        "register",
        "dashboard",
        "settings",
        "billing",
        # Local Django apps + key API route roots that are unlikely to clash
        # with realistic organization names.
        "accounts",
        "account",
        "questionnaires",
        "telegram",
        "notifications",
        "geo",
        "wallet",
        "organization",
        "organizations",
        "permissions",
        "preferences",
        "exports",
        "referral",
        "stripe",
        "otp",
        # Common abuse patterns.
        "test",
    }
)
