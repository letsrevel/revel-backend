"""Cloudflare for SaaS (custom domains for white-label organizations, #1151).

Provisioning talks to the Cloudflare API only when BOTH ``CLOUDFLARE_API_TOKEN`` and
``CLOUDFLARE_ZONE_ID`` are set. Without them (self-hosting, dev, tests) a domain request only records the
CNAME instruction and platform staff activate it by hand in the Django admin.
"""

from decouple import config

CLOUDFLARE_API_TOKEN = str(config("CLOUDFLARE_API_TOKEN", default="")).strip()
CLOUDFLARE_ZONE_ID = str(config("CLOUDFLARE_ZONE_ID", default="")).strip()
# The fallback-origin hostname organizers CNAME their custom domain to (e.g. customers.letsrevel.io).
CUSTOM_DOMAIN_CNAME_TARGET = str(config("CUSTOM_DOMAIN_CNAME_TARGET", default="")).strip().lower().rstrip(".")

# Turnstile hostname automation: the registration widget (TURNSTILE_SITE_KEY) only renders on hostnames in its
# allowlist, so Revel keeps the ACTIVE custom domains in it. On only when this, CLOUDFLARE_API_TOKEN (needs the
# account-level "Turnstile Sites: Edit" permission) and TURNSTILE_SITE_KEY are all set.
CLOUDFLARE_ACCOUNT_ID = str(config("CLOUDFLARE_ACCOUNT_ID", default="")).strip()
# Hostnames one widget can hold: 10 on the Free plan, 200 on Enterprise
# (https://developers.cloudflare.com/turnstile/plans/). 0 turns the capacity check off.
TURNSTILE_WIDGET_HOSTNAME_LIMIT = int(str(config("TURNSTILE_WIDGET_HOSTNAME_LIMIT", default="10")))
# Hard cap on live (pending + active) custom domains platform-wide; a new request is refused at the limit (409).
# 9 because the Turnstile Free plan allows 10 hostnames per widget and the platform host (letsrevel.io) takes one.
# 0 means unlimited: set it when Turnstile is off, or its plan allows more hostnames.
CUSTOM_DOMAIN_LIMIT = int(str(config("CUSTOM_DOMAIN_LIMIT", default="9")))
# Registrable domains reserved for the platform (comma-separated, e.g. ``example.co.uk``): no organization may
# claim them or anything under them. Empty derives the apex from the last two labels of the platform URLs, which
# is wrong when the platform host sits under a multi-part public suffix such as ``co.uk``; set it there (#1177).
PLATFORM_APEX_DOMAINS = tuple(
    domain
    for part in str(config("PLATFORM_APEX_DOMAINS", default="")).split(",")
    if (domain := part.strip().lower().rstrip("."))
)
