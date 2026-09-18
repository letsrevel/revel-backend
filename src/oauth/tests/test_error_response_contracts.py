"""Wire-contract tests for the OAuth provider's error bodies (spec §15, R-24/R-41/R-44).

Third of a family: ``events/tests/test_controllers/test_error_response_contracts.py`` pins
the events app's 400 bodies and ``integrations/tests/test_error_response_contracts.py`` pins
``IntegrationError``; this file pins the five shapes ``oauth/exception_handlers.py`` installs.
It lives in ``src/oauth/tests`` for the same reason the integrations one lives in its own app:
the fixtures it needs (``session_client``, ``oauth_app``, the autouse ``oauth_provider``) are
this package's and are not visible elsewhere.

Why these need pinning beyond ``test_exception_handlers.py``, which already invokes each
handler directly:

- ``AuthorizationRequestError`` renders ``{"detail", "error"}`` — a shape no other handler in
  this repo emits, and deliberately **not** RFC 6749 §5.2's ``{error, error_description}``,
  because the consumer is our own consent page rather than a third-party client (R-71). A
  future edit "towards the RFC" would break the frontend silently, so the shape is pinned
  here at the wire, where the frontend reads it.
- The consent-ticket refusals split one status across two machine-readable codes the frontend
  branches on: ``consent_required`` means "show the screen again", ``invalid_request`` means
  retrying unchanged will not help (R-74).
- Both ``WWW-Authenticate`` challenges carry information no response body does, and one of
  them must *omit* a parameter (R-27: an unscoped ``PermissionKey`` must never be advertised
  as a scope no client can request).
- The 401-vs-403 asymmetry on a staff-flagged route is a recorded decision (R-44), not an
  accident, and pinning it is what keeps it one.
"""

import json
import typing as t

import pytest
from django.http import HttpRequest
from django.test.client import Client, RequestFactory
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from common.auth_base import PermissionDenied
from common.authentication import InvalidBearerToken, ScopedJWTAuth
from events.models import Organization
from oauth.exception_handlers import HANDLERS
from oauth.models import OAuthApplication
from oauth.service import authorize_service
from oauth.tests.test_auth_class import make_access_token
from oauth.tests.test_flow import authorize_query, authorize_url, describe_consent, pkce

pytestmark = pytest.mark.django_db

# Any route on the switched surface works; this one is gated by ``manage_members``, so a
# token holding only ``org:read`` is refused by the scope gate rather than by the org
# permission map (see ``test_scope_enforcement.py`` for the full route table).
MEMBERS_ROUTE = "/api/organization-admin/{slug}/staff"
# ``edit_organization`` is the one key in ``UNSCOPED_KEYS``; this is its wire-reachable route.
UNSCOPED_ROUTE = "/api/organization-admin/{slug}/delete-logo"


def assert_authorization_error_body(response: t.Any, error: str) -> dict[str, t.Any]:
    """Assert a 400 whose body is exactly ``{"detail", "error"}`` with ``error``."""
    assert response.status_code == 400, response.content
    body = t.cast(dict[str, t.Any], response.json())
    assert set(body) == {"detail", "error"}, body
    assert isinstance(body["detail"], str) and body["detail"], body
    assert body["error"] == error, body
    # Not the RFC 6749 §5.2 shape, on purpose (R-71) — pinned so the deviation stays deliberate.
    assert "error_description" not in body, body
    return body


def assert_detail_only_body(response: t.Any, status_code: int) -> str:
    """Assert ``status_code`` with a body that is exactly ``{"detail": "<non-empty>"}``."""
    assert response.status_code == status_code, response.content
    body = t.cast(dict[str, t.Any], response.json())
    assert set(body) == {"detail"}, body
    assert isinstance(body["detail"], str) and body["detail"], body
    return body["detail"]


def _app_client(user: RevelUser, app: OAuthApplication, scopes: str, **kwargs: t.Any) -> Client:
    """A client authenticated with an app token granting exactly ``scopes``."""
    return Client(HTTP_AUTHORIZATION=f"Bearer {make_access_token(user, app, scopes, **kwargs)}")


class TestAuthorizationRequestErrorContracts:
    """``GET /api/oauth/authorize`` refusals: ``{"detail", "error"}``, never a redirect (R-71)."""

    def test_invalid_scope_returns_detail_and_error(
        self, session_client: Client, public_oauth_app: OAuthApplication
    ) -> None:
        _, challenge = pkce()
        response = session_client.get(
            "/api/oauth/authorize", authorize_query(public_oauth_app, challenge, "openid not_a_scope")
        )
        assert_authorization_error_body(response, "invalid_scope")

    def test_missing_pkce_returns_detail_and_error(
        self, session_client: Client, public_oauth_app: OAuthApplication
    ) -> None:
        """PKCE is mandatory (OAuth 2.1), and its absence is not redirected either."""
        query = authorize_query(public_oauth_app, "", "openid", code_challenge="", code_challenge_method="")
        response = session_client.get("/api/oauth/authorize", query)
        assert_authorization_error_body(response, "invalid_request")


class TestConsentTicketErrorContracts:
    """``POST /api/oauth/authorize`` distinguishes "re-show the screen" from "this is wrong".

    The frontend branches on ``error``: ``consent_required`` re-renders the consent screen,
    ``invalid_request`` is terminal. Both are 400 with the same body shape, so the code is
    the only discriminator and must not drift.
    """

    @staticmethod
    def _decide(session_client: Client, app: OAuthApplication, challenge: str, scope: str, **body: t.Any) -> t.Any:
        """POST a decision body verbatim, bypassing the happy-path ticket plumbing."""
        return session_client.post(authorize_url(app, challenge, scope), data=body, content_type="application/json")

    def test_missing_ticket_is_consent_required(
        self, session_client: Client, public_oauth_app: OAuthApplication
    ) -> None:
        _, challenge = pkce()
        response = self._decide(session_client, public_oauth_app, challenge, "org:read", allow=True)
        assert_authorization_error_body(response, "consent_required")

    def test_expired_ticket_is_consent_required(
        self, monkeypatch: pytest.MonkeyPatch, session_client: Client, public_oauth_app: OAuthApplication
    ) -> None:
        _, challenge = pkce()
        ticket = describe_consent(session_client, public_oauth_app, challenge, "org:read")["consent_ticket"]
        monkeypatch.setattr(authorize_service, "CONSENT_TICKET_TTL_SECONDS", -1)
        response = self._decide(
            session_client, public_oauth_app, challenge, "org:read", allow=True, consent_ticket=ticket
        )
        assert_authorization_error_body(response, "consent_required")

    def test_forged_ticket_is_invalid_request(self, session_client: Client, public_oauth_app: OAuthApplication) -> None:
        _, challenge = pkce()
        ticket = describe_consent(session_client, public_oauth_app, challenge, "org:read")["consent_ticket"]
        forged = ticket[:-1] + ("A" if ticket[-1] != "A" else "B")
        response = self._decide(
            session_client, public_oauth_app, challenge, "org:read", allow=True, consent_ticket=forged
        )
        assert_authorization_error_body(response, "invalid_request")

    def test_ticket_for_another_grant_is_invalid_request(
        self, session_client: Client, public_oauth_app: OAuthApplication
    ) -> None:
        """A genuine ticket that describes different scopes than the decision asks for."""
        _, challenge = pkce()
        ticket = describe_consent(session_client, public_oauth_app, challenge, "org:read")["consent_ticket"]
        response = self._decide(
            session_client, public_oauth_app, challenge, "org:read org:events", allow=True, consent_ticket=ticket
        )
        assert_authorization_error_body(response, "invalid_request")


class TestInsufficientScopeContracts:
    """403 with an RFC 6750 §3.1 challenge, in both its forms."""

    def test_missing_scope_names_the_scope_in_the_challenge(
        self, organization: Organization, oauth_app: OAuthApplication
    ) -> None:
        client = _app_client(organization.owner, oauth_app, "org:read")
        response = client.get(MEMBERS_ROUTE.format(slug=organization.slug))
        assert_detail_only_body(response, 403)
        assert response["WWW-Authenticate"] == 'Bearer error="insufficient_scope", scope="org:members"'

    def test_unscoped_key_omits_the_scope_parameter(
        self, organization: Organization, oauth_app: OAuthApplication
    ) -> None:
        """R-27: ``edit_organization`` maps to no scope, so the challenge names none.

        ``oauth_app`` is granted *every* scope in the vocabulary and the caller owns the
        organization, so the only thing that can refuse this request is the unscoped-key
        short-circuit — which makes the absent ``scope=`` parameter the assertion's subject
        rather than a side effect of a narrow token.
        """
        client = _app_client(organization.owner, oauth_app, " ".join(sorted(oauth_app.allowed_scopes)))
        response = client.delete(UNSCOPED_ROUTE.format(slug=organization.slug))
        assert_detail_only_body(response, 403)
        assert response["WWW-Authenticate"] == 'Bearer error="insufficient_scope"'
        assert "scope=" not in response["WWW-Authenticate"]


class TestInvalidBearerTokenContracts:
    """401 with the RFC 9728 ``resource_metadata`` pointer, which is how MCP hosts recover."""

    def test_expired_app_token_advertises_the_resource_metadata(
        self, organization: Organization, oauth_app: OAuthApplication
    ) -> None:
        client = _app_client(organization.owner, oauth_app, "org:read", expires_in=-10)
        response = client.get(MEMBERS_ROUTE.format(slug=organization.slug))
        assert_detail_only_body(response, 401)
        assert response["WWW-Authenticate"] == (
            'Bearer error="invalid_token", resource_metadata="http://testserver/.well-known/oauth-protected-resource"'
        )

    def test_challenge_omits_resource_metadata_without_an_issuer(
        self, settings: t.Any, organization: Organization, oauth_app: OAuthApplication
    ) -> None:
        """RFC 9728 §5.1 wants an absolute URI, so an unset issuer drops the parameter."""
        settings.OAUTH_ISSUER = ""
        client = _app_client(organization.owner, oauth_app, "org:read", expires_in=-10)
        response = client.get(MEMBERS_ROUTE.format(slug=organization.slug))
        assert_detail_only_body(response, 401)
        assert response["WWW-Authenticate"] == 'Bearer error="invalid_token"'


class TestStaffFlaggedRouteAsymmetry:
    """R-44: a staff-flagged route refuses a session token with 403 and an app token with 401.

    No route switched in v1 is staff-flagged, so this is defensive code with no endpoint to
    exercise — the same situation as ``TestOrganizerRefundExceptionContracts`` in the events
    file, and handled the same way: drive the auth class directly and render through the
    registered handler. The asymmetry is deliberate (the *token* is unusable on such a route,
    whatever the user's flags, so the app side is 401 ``invalid_token`` rather than 403), and
    it is pinned here so a future change to it is a decision rather than a regression.
    """

    @staticmethod
    def _authenticate(token: str) -> t.Any:
        request = RequestFactory().get("/api/x", HTTP_AUTHORIZATION=f"Bearer {token}")
        return ScopedJWTAuth(is_staff=True)(request)

    def test_session_token_is_403(self, user: RevelUser) -> None:
        access = str(RefreshToken.for_user(user).access_token)  # type: ignore[attr-defined]
        with pytest.raises(PermissionDenied) as exc:
            self._authenticate(access)
        assert exc.value.status_code == 403

    def test_app_token_is_401_with_an_invalid_token_challenge(
        self, user: RevelUser, oauth_app: OAuthApplication
    ) -> None:
        raw = make_access_token(user, oauth_app, "org:read")
        with pytest.raises(InvalidBearerToken) as exc:
            self._authenticate(raw)
        response = HANDLERS[InvalidBearerToken](HttpRequest(), exc.value)
        assert response.status_code == 401
        assert json.loads(response.content) == {"detail": "Invalid or expired token."}
        assert response["WWW-Authenticate"].startswith('Bearer error="invalid_token"')


class TestProviderDisabledContract:
    """With no signing key the whole provider is a 404 — the app-agnostic ``{"detail"}`` shape."""

    def test_authorize_404s_when_the_provider_is_off(self, settings: t.Any, session_client: Client) -> None:
        settings.OIDC_SIGNING_KEY_PATH = ""
        _, challenge = pkce()
        response = session_client.get("/api/oauth/authorize", authorize_query_for_disabled(challenge))
        assert_detail_only_body(response, 404)


def authorize_query_for_disabled(challenge: str) -> dict[str, str]:
    """A syntactically complete authorize query that needs no registered application.

    The disabled check runs before any client lookup, so the parameters only have to be
    present — which keeps the test about the 404 rather than about client resolution.
    """
    return {
        "client_id": "unknown",
        "response_type": "code",
        "redirect_uri": "https://app.example/cb",
        "scope": "openid",
        "state": "xyz",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }


class TestAppLimitContract:
    """The developer-portal cap is a 409 with a plain ``{"detail"}`` body."""

    def test_cap_returns_409_detail(self, settings: t.Any, session_client: Client) -> None:
        settings.OAUTH_MAX_APPS_PER_USER = 0
        response = session_client.post(
            "/api/oauth/apps/",
            data={
                "name": "My App",
                "client_type": "confidential",
                "redirect_uris": ["https://x.example/cb"],
                "allowed_scopes": ["openid"],
            },
            content_type="application/json",
        )
        assert_detail_only_body(response, 409)


def test_handler_map_is_exactly_the_documented_set() -> None:
    """Every shape above has a handler, and no handler above is unpinned.

    The five entries are the whole oauth response map; a sixth added without a contract test
    fails here, which is the only thing that keeps this file honest as the app grows.
    """
    assert {cls.__name__ for cls in HANDLERS} == {
        "OAuthProviderDisabledError",
        "AuthorizationRequestError",
        "InvalidBearerToken",
        "InsufficientScopeError",
        "AppLimitReachedError",
    }
