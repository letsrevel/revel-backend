"""Discovery documents, JWKS and the feature-flag gate on every protocol route (spec §8.1)."""

import typing as t

import pytest
from django.test.client import Client
from django.urls import reverse

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
