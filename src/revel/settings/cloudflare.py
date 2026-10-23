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
