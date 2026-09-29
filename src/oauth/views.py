"""Thin subclasses of DOT's protocol views (spec §8.1).

Everything user-facing lives elsewhere: the consent screen is a ninja controller, and DOT's
own session-based authorize view is never mounted — both discovery documents advertise the
SvelteKit page instead. Gating, throttling and caching are applied per route in ``urls.py``,
so that module is the single readable table of what protects each endpoint.
"""

import functools
import typing as t

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from oauth2_provider.views import (
    ConnectDiscoveryInfoView,
    DynamicClientRegistrationView,
    OAuthServerMetadataView,
)

from oauth.utils import oauth_provider_enabled

# The dynamic-registration counter is keyed per UTC day. A 26-hour TTL covers the 24-hour
# window plus clock skew between app servers, so today's bucket can never expire while it is
# still today (which would silently reset the cap mid-day).
_DCR_COUNTER_TTL_SECONDS = 26 * 3600


def provider_required(view: t.Callable[..., HttpResponse]) -> t.Callable[..., HttpResponse]:
    """404 unless the OAuth provider is enabled.

    Credential presence is the feature flag (ADR-0008), and with it unset every protocol
    route must 404. DOT's own ``OIDCOnlyMixin`` covers only its OIDC views, keys off
    ``OIDC_ENABLED`` rather than the signing key, and raises ``ImproperlyConfigured`` instead
    of 404 while ``DEBUG`` is on — so this is applied uniformly to every route.

    ``functools.wraps`` is load-bearing, not cosmetic: ``View.as_view()`` copies the attributes
    decorators leave on ``dispatch`` onto the view function, and ``CsrfViewMiddleware`` reads
    ``csrf_exempt`` off that function. A wrapper that did not copy ``__dict__`` would silently
    re-arm CSRF on DOT's ``csrf_exempt`` protocol views, 403-ing every POST to ``/o/token``.

    Args:
        view: The view to wrap.

    Returns:
        The wrapped view.
    """

    @functools.wraps(view)
    def wrapped(request: HttpRequest, *args: t.Any, **kwargs: t.Any) -> HttpResponse:
        if not oauth_provider_enabled():
            return JsonResponse({"error": "not_found"}, status=404)
        return view(request, *args, **kwargs)

    return wrapped


class _FrontendAuthorizeMixin:
    """Point ``authorization_endpoint`` at the SvelteKit consent page.

    DOT's ``AuthorizationView`` is deliberately not mounted (spec §8.1), so ``reverse()`` on
    its URL name would raise ``NoReverseMatch`` and both discovery documents would fail.
    """

    def _get_endpoint_url(self, request: HttpRequest, view_name: str, required: bool = False) -> str | None:
        """Mirror of ``ServerMetadataViewMixin._get_endpoint_url`` for the authorize endpoint only."""
        if view_name == "authorize":
            return f"{settings.FRONTEND_BASE_URL.rstrip('/')}/oauth/authorize"
        return t.cast(str | None, super()._get_endpoint_url(request, view_name, required))  # type: ignore[misc]


class RevelConnectDiscoveryInfoView(_FrontendAuthorizeMixin, ConnectDiscoveryInfoView):  # type: ignore[misc]
    """OIDC discovery document with our frontend as the authorization endpoint."""


class RevelAuthorizationServerMetadataView(_FrontendAuthorizeMixin, OAuthServerMetadataView):  # type: ignore[misc]
    """RFC 8414 metadata with our frontend as the authorization endpoint."""


class RevelDynamicClientRegistrationView(DynamicClientRegistrationView):  # type: ignore[misc]
    """DOT's DCR view plus an instance-wide daily cap and a hard "no owner" rule.

    Per-IP throttling is applied in ``urls.py``; the cap below is the instance-wide backstop
    a botnet spread across many IPs would otherwise walk past.
    """

    def post(self, request: HttpRequest, *args: t.Any, **kwargs: t.Any) -> HttpResponse:
        """Register a client, refusing once the instance's daily cap is spent."""
        key = f"oauth:dcr:{timezone.now():%Y%m%d}"
        cache.add(key, 0, timeout=_DCR_COUNTER_TTL_SECONDS)
        if cache.incr(key) > settings.OAUTH_DCR_DAILY_CAP:
            cache.decr(key)
            return JsonResponse(
                {"error": "slow_down", "error_description": str(_("Registration cap reached."))}, status=429
            )
        # spec §6.1: a dynamically registered app has no owner, and ``OAuthApplication.clean()``
        # enforces that. DOT's view would otherwise copy a session-authenticated ``request.user``
        # onto the application and fail its own ``full_clean()`` with 400 invalid_client_metadata
        # (R-34/R-58) — reachable from any browser logged into ``/admin/``. Blanking the user here
        # is safe because we ship ``AllowAllDCRPermission``; a deployment that swaps in
        # ``IsAuthenticatedDCRPermission`` would have to move this below the permission check.
        request.user = AnonymousUser()
        response = t.cast(HttpResponse, super().post(request, *args, **kwargs))
        # The cap is a budget of registrations, not of attempts: a few dozen IPs sending
        # deliberate garbage would otherwise burn the instance-wide allowance for the rest of
        # the UTC day and lock out legitimate clients. Refunding a rejected attempt keeps the
        # counter and the rows in step; the per-IP throttle is what limits garbage.
        if response.status_code != 201:
            cache.decr(key)
        return response
