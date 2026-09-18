"""Discovery documents, JWKS and the feature-flag gate on every protocol route (spec §8.1)."""

import re
import typing as t

import pytest
from django.test.client import Client
from django.urls import reverse

from api.api import api
from oauth.controllers import OAUTH_CONTROLLERS
from oauth.permissions import ProviderEnabled

pytestmark = pytest.mark.django_db


def test_dot_can_reverse_its_own_namespace() -> None:
    """R-08: a doubled namespace would break DOT's internal ``reverse("oauth2_provider:...")``."""
    assert reverse("oauth2_provider:token") == "/o/token"
    assert reverse("oauth2_provider:jwks-info") == "/o/jwks"
    assert reverse("oauth2_provider:user-info") == "/o/userinfo"
    assert reverse("oauth2_provider:revoke-token") == "/o/revoke"
    assert reverse("oauth2_provider:dcr-register") == "/o/register"


def test_openid_configuration(client: Client) -> None:
    data = client.get("/.well-known/openid-configuration").json()
    assert data["issuer"] == "http://testserver"
    assert data["authorization_endpoint"] == "http://frontend.test/oauth/authorize"
    assert data["token_endpoint"] == "http://testserver/o/token"
    assert data["jwks_uri"] == "http://testserver/o/jwks"
    assert data["userinfo_endpoint"] == "http://testserver/o/userinfo"
    assert "org:read" in data["scopes_supported"]
    assert "RS256" in data["id_token_signing_alg_values_supported"]


def test_authorization_server_metadata(client: Client) -> None:
    data = client.get("/.well-known/oauth-authorization-server").json()
    assert data["authorization_endpoint"] == "http://frontend.test/oauth/authorize"
    assert data["registration_endpoint"] == "http://testserver/o/register"
    assert data["revocation_endpoint"] == "http://testserver/o/revoke"
    assert "introspection_endpoint" not in data
    assert "S256" in data["code_challenge_methods_supported"]


def test_protected_resource_metadata(client: Client) -> None:
    data = client.get("/.well-known/oauth-protected-resource").json()
    assert data["resource"] == "http://testserver"
    assert data["authorization_servers"] == ["http://testserver"]
    assert data["bearer_methods_supported"] == ["header"]


def test_jwks_publishes_rsa_key(client: Client) -> None:
    keys = client.get("/o/jwks").json()["keys"]
    assert keys and keys[0]["kty"] == "RSA"


def test_documents_are_cacheable(client: Client) -> None:
    for path in (
        "/.well-known/openid-configuration",
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
    ):
        assert "max-age=3600" in client.get(path)["Cache-Control"], path


class _ProviderRoute(t.NamedTuple):
    """One registered ``/api/oauth`` operation, flattened for assertion."""

    method: str
    url: str
    permissions: list[t.Any]
    has_request_body: bool


def _api_provider_routes() -> list[_ProviderRoute]:
    """Every ``/api/oauth/...`` operation, derived from the live API registry.

    Derived rather than listed (R-124): the hand-written list that used to live in this file is
    exactly why ``/api/oauth/apps`` and ``/api/oauth/connections`` shipped half-live with the
    provider disabled. Anything registered from ``OAUTH_CONTROLLERS`` is covered the moment it
    exists, so a future provider route cannot be forgotten.

    Returns:
        One entry per method, with a UUID substituted for every path parameter (both live
        parameters, ``app_id`` and ``client_id``, accept one) and the route's *effective*
        permission list — ninja-extra resolves it as ``route.permissions or
        controller.permission_classes``, so a route-level list would replace the class-level one.
    """
    placeholder = "11111111-1111-1111-1111-111111111111"
    provider_controllers = set(OAUTH_CONTROLLERS)
    body_sources = {"body", "file", "form"}
    routes: list[_ProviderRoute] = []
    for prefix, router in api._routers:  # noqa: SLF001
        if getattr(router, "controller_class", None) not in provider_controllers:
            continue
        for path, path_view in router.path_operations.items():
            url = re.sub(r"\{[^}]+\}", placeholder, f"/api{prefix}{path}")
            for operation in path_view.operations:
                # Every provider route is a controller route, so ninja-extra always attached this.
                route_function = t.cast(t.Any, operation.view_func).get_route_function()
                route = route_function.route
                permissions = list(route.permissions or route_function.api_controller.permission_classes or [])
                has_body = any(
                    getattr(model, "__ninja_param_source__", None) in body_sources
                    for model in operation.signature.models
                )
                routes.extend(
                    _ProviderRoute(method, url, permissions, has_body) for method in sorted(operation.methods)
                )
    return sorted(routes, key=lambda r: (r.url, r.method))


API_PROVIDER_ROUTES = _api_provider_routes()
# Routes ninja can answer without a request body. Ninja validates the body *before* ninja-extra
# runs the permission layer, so a POST whose payload is required answers 422 rather than 404 with
# the provider off — a wrong body is refused whether or not the provider exists. Those routes are
# covered by the permission-list property instead, which is the one that cannot forget a route.
BODYLESS_PROVIDER_ROUTES = [r for r in API_PROVIDER_ROUTES if not r.has_request_body]


def test_the_derived_provider_route_lists_are_not_empty() -> None:
    """Parametrized guards that skip every case assert nothing (R-15)."""
    assert len(API_PROVIDER_ROUTES) >= 13, API_PROVIDER_ROUTES
    assert len(BODYLESS_PROVIDER_ROUTES) >= 9, BODYLESS_PROVIDER_ROUTES


@pytest.mark.parametrize("route", API_PROVIDER_ROUTES, ids=lambda r: f"{r.method} {r.url}")
def test_every_api_provider_route_is_gated_by_the_feature_flag(route: _ProviderRoute) -> None:
    """Issue #986's acceptance criterion, as a property of the route tree (R-124).

    The status-code guard below cannot cover the three routes with a required request body, so
    this is the one that is total: a provider route registered without ``ProviderEnabled`` fails
    here whatever its payload looks like.
    """
    assert any(isinstance(p, ProviderEnabled) for p in route.permissions), (
        f"{route.method} {route.url} is a provider route but does not declare ProviderEnabled(), "
        f"so it stays live with OIDC_SIGNING_KEY_PATH unset."
    )


@pytest.mark.parametrize("route", BODYLESS_PROVIDER_ROUTES, ids=lambda r: f"{r.method} {r.url}")
def test_disabled_provider_404s_every_api_route(route: _ProviderRoute, settings: t.Any, session_client: Client) -> None:
    """With no signing key, the flag really does answer 404 — not merely declare it.

    Authenticated on purpose: a 401 would satisfy a "not 200" check while proving nothing about
    the flag. The provider being off must be indistinguishable from the route not existing.
    """
    settings.OIDC_SIGNING_KEY_PATH = ""
    settings.OAUTH2_PROVIDER = {**settings.OAUTH2_PROVIDER, "OIDC_ENABLED": False, "DCR_ENABLED": False}
    response = session_client.generic(route.method, route.url, data="", content_type="application/json")
    assert response.status_code == 404, (route.method, route.url, response.status_code, response.content)


def test_disabled_provider_404s(settings: t.Any, client: Client) -> None:
    """Every protocol route 404s with no signing key configured (ADR-0008, issue acceptance)."""
    settings.OIDC_SIGNING_KEY_PATH = ""
    settings.OAUTH2_PROVIDER = {**settings.OAUTH2_PROVIDER, "OIDC_ENABLED": False, "DCR_ENABLED": False}
    assert client.get("/.well-known/openid-configuration").status_code == 404
    assert client.get("/.well-known/oauth-authorization-server").status_code == 404
    assert client.get("/.well-known/oauth-protected-resource").status_code == 404
    assert client.get("/o/jwks").status_code == 404
    assert client.get("/o/userinfo").status_code == 404
    assert client.post("/o/token", {}).status_code == 404
    assert client.post("/o/revoke", {}).status_code == 404
    assert client.post("/o/register", data="{}", content_type="application/json").status_code == 404
    assert client.get("/o/register/whatever").status_code == 404


def test_userinfo_requires_a_token(client: Client) -> None:
    """The route is mounted and reachable; without a bearer token DOT refuses it."""
    assert client.get("/o/userinfo").status_code == 401


def test_protocol_posts_stay_csrf_exempt() -> None:
    """DOT's POST endpoints are ``csrf_exempt``; the gating wrappers must not re-arm CSRF.

    ``View.as_view()`` copies that flag off ``dispatch`` onto the view function, so a wrapper
    that dropped ``__dict__`` would 403 every token/registration POST in production. The
    default test client suppresses CSRF entirely, hence ``enforce_csrf_checks``.
    """
    csrf_client = Client(enforce_csrf_checks=True)
    # Exact statuses, not ``!= 403``: that would also pass on a 404 from a renamed route.
    assert csrf_client.post("/o/token", {}).status_code == 400
    assert csrf_client.post("/o/revoke", {}).status_code == 400
    assert csrf_client.post("/o/register", data="{}", content_type="application/json").status_code == 400
    # PUT/DELETE are unsafe methods too, and the round-trip test uses the default client,
    # which suppresses CSRF — so these two are covered nowhere else.
    assert csrf_client.put("/o/register/whatever", data="{}", content_type="application/json").status_code == 401
    assert csrf_client.delete("/o/register/whatever").status_code == 401
