"""Cloudflare Turnstile (bot check on registration).

Both keys empty → feature off (dev, e2e, demo, self-hosters). The feature is on only when BOTH
are set; a half-configured pair is treated as off so a typo can't block every signup.
"""

import warnings

from decouple import config

TURNSTILE_SITE_KEY = str(config("TURNSTILE_SITE_KEY", default="")).strip()
TURNSTILE_SECRET_KEY = str(config("TURNSTILE_SECRET_KEY", default="")).strip()

if bool(TURNSTILE_SITE_KEY) != bool(TURNSTILE_SECRET_KEY):
    warnings.warn(
        "Only one of TURNSTILE_SITE_KEY / TURNSTILE_SECRET_KEY is set; Turnstile stays disabled.",
        stacklevel=1,
    )
