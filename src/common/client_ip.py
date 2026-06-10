"""Single source of truth for resolving the real client IP of a request.

Production network path: Cloudflare → Caddy → gunicorn. Caddy is configured
(infra ``Caddyfile``) with ``trusted_proxies`` for Cloudflare's ranges and
``client_ip_headers Cf-Connecting-Ip``, and overwrites both ``X-Real-IP`` and
``X-Forwarded-For`` with the single resolved visitor IP on every request. That
makes ``X-Real-IP`` trustworthy: a client can never set it, and a peer hitting
the origin directly gets its own socket IP.

Do NOT read ``CF-Connecting-IP`` here: Caddy strips it, and trusting it in
Django would be spoofable by anyone reaching the origin directly. Do NOT trust
arbitrary ``X-Forwarded-For`` positions either — the first entry is
client-controlled in generic setups. See backend issue #479 / infra #3.
"""

from django.http import HttpRequest


def get_client_ip(request: HttpRequest) -> str:
    """Return the real client IP for a request, or ``""`` if unknown.

    Trusts ``X-Real-IP`` (set by Caddy from the Cloudflare-resolved client IP),
    falling back to ``REMOTE_ADDR`` for local development, tests, and internal
    calls that don't pass through Caddy's Django proxy block.
    """
    if x_real_ip := request.META.get("HTTP_X_REAL_IP"):
        return str(x_real_ip).strip()
    return str(request.META.get("REMOTE_ADDR", ""))
