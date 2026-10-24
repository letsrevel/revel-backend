"""Caddy on-demand TLS "ask" endpoint for white-label custom domains (#1178).

Self-hosted instances that issue certificates with Caddy's on-demand TLS configure
``on_demand_tls { ask http://<backend>/api/tls/ask }``. Before obtaining a certificate for a
hostname Caddy calls ``GET <ask>?domain=<host>`` and only proceeds on a 200.
"""

from django.http import HttpRequest, HttpResponse
from ninja_extra import api_controller, route

from events.service import tenant_service


@api_controller("/tls", tags=["TLS"])
class TlsAskController:
    """Answers Caddy's certificate "ask" check; not meant for clients."""

    @route.get(
        "/ask",
        url_name="tls_ask",
        response={200: None, 404: None},
        include_in_schema=False,
        # Unthrottled on purpose. Every ask reaches us from the one proxy IP, so the anonymous
        # per-IP throttle (250/day) would be spent by junk SNI handshakes and starve real tenants.
        # The endpoint is a cache lookup (negative answers are cached too, see ``tenant_service``),
        # and it reveals nothing beyond the status code, which ``by-domain`` already exposes publicly.
        # ``throttle=[]`` (not ``None``) is how ninja spells "no throttles".
        throttle=[],
    )
    def tls_ask(self, request: HttpRequest, domain: str = "") -> HttpResponse:
        """Allow certificate issuance only for an ACTIVE custom domain of an enabled organization.

        Args:
            request: The request from Caddy.
            domain: The hostname Caddy wants a certificate for (appended by Caddy as ``?domain=``).

        Returns:
            200 with an empty body for an active tenant hostname whose organization has the
            ``custom_domain_enabled`` kill switch on; 404 for anything else (unknown, pending,
            failed or removed, kill switch off, platform host, malformed, missing or over-long).
        """
        return HttpResponse(status=200 if tenant_service.is_tenant_origin(domain) else 404)
