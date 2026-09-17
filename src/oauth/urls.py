"""DOT protocol routes, in the ``oauth2_provider`` namespace DOT reverses (spec §8.1).

This module is the single table of what protects each protocol endpoint. Every route is
wrapped in ``provider_required`` (outermost, so a disabled provider 404s before spending any
throttle budget or emitting cache headers), then throttled, then cached.

NO module-level ``app_name`` (R-08): Django would derive a namespace from it *and* from the
``include()`` below, making the effective namespace ``oauth2_provider:oauth2_provider`` and
breaking DOT's internal ``reverse("oauth2_provider:token")``. ``test_discovery`` pins this.

DOT's own patterns use trailing slashes; ours do not, and ``reverse()`` returns whatever we
registered, so the discovery documents stay consistent either way.

Deliberately not mounted: DOT's session-based ``AuthorizationView`` (the consent screen is a
ninja controller, and discovery points at the SvelteKit page), token introspection,
RP-initiated logout and the device flow.
"""

from django.urls import include, path
from django.views.decorators.cache import cache_control
from oauth2_provider import views as dot

from common.throttling import OAuthRegistrationThrottle, OAuthTokenThrottle, throttled
from oauth import views

_CACHE_1H = cache_control(public=True, max_age=3600)

_patterns = [
    path(
        ".well-known/openid-configuration",
        views.provider_required(_CACHE_1H(views.RevelConnectDiscoveryInfoView.as_view())),
        name="oidc-connect-discovery-info",
    ),
    path(
        ".well-known/oauth-authorization-server",
        views.provider_required(_CACHE_1H(views.RevelAuthorizationServerMetadataView.as_view())),
        name="oauth-server-metadata",
    ),
    path(
        ".well-known/oauth-protected-resource",
        views.provider_required(_CACHE_1H(dot.OAuthProtectedResourceMetadataView.as_view())),
        name="oauth-resource-metadata",
    ),
    # No cache_control here: DOT's JwksInfoView already sets its own
    # ``public, max-age=OIDC_JWKS_MAX_AGE_SECONDS`` (3600) with stale-while-revalidate.
    path("o/jwks", views.provider_required(dot.JwksInfoView.as_view()), name="jwks-info"),
    path(
        "o/token",
        views.provider_required(throttled(OAuthTokenThrottle)(dot.TokenView.as_view())),
        name="token",
    ),
    path(
        "o/revoke",
        views.provider_required(throttled(OAuthTokenThrottle)(dot.RevokeTokenView.as_view())),
        name="revoke-token",
    ),
    path(
        "o/userinfo",
        views.provider_required(throttled(OAuthTokenThrottle)(dot.UserInfoView.as_view())),
        name="user-info",
    ),
    path(
        "o/register",
        views.provider_required(
            throttled(OAuthRegistrationThrottle)(views.RevelDynamicClientRegistrationView.as_view())
        ),
        name="dcr-register",
    ),
    path(
        "o/register/<str:client_id>",
        views.provider_required(throttled(OAuthTokenThrottle)(dot.DynamicClientRegistrationManagementView.as_view())),
        name="dcr-register-management",
    ),
]

urlpatterns = [path("", include((_patterns, "oauth2_provider"), namespace="oauth2_provider"))]
