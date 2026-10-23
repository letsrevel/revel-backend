"""Tenant-host session binding: mint, refresh and access-token enforcement (white-label, #1148).

A session issued while serving a custom domain carries ``tenant_host`` / ``tenant_session_iat`` and
dies with the domain, so an organization owner who repoints the domain's DNS at their own server
cannot harvest live sessions from the refresh cookies the browsers send them.
"""

import time
import typing as t
from datetime import timedelta
from unittest.mock import patch

import orjson
import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.test import RequestFactory
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.exceptions import InvalidToken
from ninja_jwt.token_blacklist.models import BlacklistedToken
from ninja_jwt.tokens import AccessToken, RefreshToken

from accounts.models import RevelUser
from accounts.service import auth as auth_service
from common.auth_base import SessionRevoked
from common.authentication import I18nJWTAuth, OptionalAuth, ScopedJWTAuth
from events.models import Organization, OrganizationDomain
from events.service import cloudflare_custom_hostnames as cloudflare
from events.service import custom_domain_service

pytestmark = pytest.mark.django_db

HOST = "events.example.com"
TENANT_ORIGIN = f"https://{HOST}"
PLATFORM_ORIGIN = "https://app.letsrevel.io"
PASSWORD = "strong-password-123!"  # noqa: S105 - test fixture password
PLAIN_CLAIMS = {"token_type", "exp", "iat", "jti", "user_id", "aud"}
TENANT_CLAIMS = {"tenant_host", "tenant_session_iat"}


@pytest.fixture
def org(django_user_model: type[RevelUser]) -> Organization:
    owner = django_user_model.objects.create_user(username="tenant_owner", email="tenant_owner@example.com")
    return Organization.objects.create(name="Tenant Org", slug="tenant-org", owner=owner, custom_domain_enabled=True)


@pytest.fixture
def domain(org: Organization) -> OrganizationDomain:
    """An ACTIVE domain that was verified an hour ago."""
    row = OrganizationDomain.objects.create(organization=org, hostname=HOST, cloudflare_hostname_id="cf-1")
    custom_domain_service.activate_domain(row)
    OrganizationDomain.objects.filter(pk=row.pk).update(verified_at=timezone.now() - timedelta(hours=1))
    cache.clear()
    return row


def _login(client: Client, user: RevelUser, origin: str | None = None) -> dict[str, str]:
    headers = {"Origin": origin} if origin else {}
    response = client.post(
        reverse("api:token_obtain_pair"),
        data=orjson.dumps({"username": user.username, "password": PASSWORD}),
        content_type="application/json",
        headers=headers,
    )
    assert response.status_code == 200, response.content
    return t.cast(dict[str, str], response.json())


def _refresh(client: Client, refresh: str, origin: str | None = None) -> t.Any:
    headers = {"Origin": origin} if origin else {}
    return client.post(
        reverse("api:token_refresh"),
        data=orjson.dumps({"refresh": refresh}),
        content_type="application/json",
        headers=headers,
    )


def _tenant_refresh(user: RevelUser, *, issued_at: int | None = None, **extra: t.Any) -> RefreshToken:
    """A refresh token bound to HOST, issued ``issued_at`` (default: 30 minutes ago, after the 1h-old verification)."""
    token = RefreshToken.for_user(user)
    token.payload.update(
        {"tenant_host": HOST, "tenant_session_iat": issued_at or int(time.time()) - 1800, **extra}  # type: ignore[attr-defined]
    )
    return token  # type: ignore[return-value]


def _blacklisted(refresh: t.Any) -> bool:
    return BlacklistedToken.objects.filter(token__jti=refresh["jti"]).exists()


# --- Revocation scenarios: each makes HOST stop being a valid tenant session ---


def _remove(domain: OrganizationDomain) -> None:
    custom_domain_service.remove_domain(domain)


def _kill_switch_off(domain: OrganizationDomain) -> None:
    Organization.objects.filter(pk=domain.organization_id).update(custom_domain_enabled=False)


def _fail(domain: OrganizationDomain) -> None:
    custom_domain_service.mark_domain_failed(domain)


def _delete_org(domain: OrganizationDomain) -> None:
    Organization.objects.get(pk=domain.organization_id).delete()


def _cloudflare_moved(domain: OrganizationDomain) -> None:
    moved = cloudflare.CustomHostname(
        id="cf-1", hostname=HOST, status="moved", ssl=cloudflare.CustomHostnameSsl(status="active")
    )
    with (
        patch.object(cloudflare, "get_custom_hostname", return_value=moved),
        patch("events.tasks.custom_domains.notify_custom_domain_problem.delay"),
    ):
        custom_domain_service.sync_domain_status(domain)


def _reactivate(domain: OrganizationDomain) -> None:
    custom_domain_service.activate_domain(domain)  # verified_at = now, later than a session issued 30 min ago


LIVE_STATE_REVOCATIONS = pytest.mark.parametrize(
    "revoke",
    [_remove, _kill_switch_off, _fail, _delete_org, _cloudflare_moved],
    ids=["removed", "kill_switch_off", "failed", "org_deleted", "cloudflare_moved"],
)


# --- Mint ---


def test_password_pair_on_tenant_origin_carries_claims(
    client: Client, user: RevelUser, domain: OrganizationDomain
) -> None:
    before = int(time.time())
    pair = _login(client, user, TENANT_ORIGIN)

    refresh, access = RefreshToken(pair["refresh"]).payload, AccessToken(pair["access"]).payload
    for payload in (refresh, access):
        assert payload["tenant_host"] == HOST
        assert before <= payload["tenant_session_iat"] <= int(time.time())
        assert set(payload) == PLAIN_CLAIMS | TENANT_CLAIMS
    assert refresh["token_type"] == "refresh"
    assert access["token_type"] == "access"


@pytest.mark.parametrize("origin", [None, PLATFORM_ORIGIN, "https://unknown.example.org"])
def test_password_pair_without_tenant_origin_is_unchanged(
    client: Client, user: RevelUser, domain: OrganizationDomain, origin: str | None
) -> None:
    pair = _login(client, user, origin)

    stock = RefreshToken(str(RefreshToken.for_user(user)))  # decoded, as the pair's tokens are below
    assert set(RefreshToken(pair["refresh"]).payload) == set(stock.payload) == PLAIN_CLAIMS
    assert set(AccessToken(pair["access"]).payload) == set(AccessToken(str(stock.access_token)).payload)  # type: ignore[attr-defined]


def test_pending_domain_origin_does_not_mint(client: Client, user: RevelUser, org: Organization) -> None:
    OrganizationDomain.objects.create(organization=org, hostname=HOST)  # PENDING

    pair = _login(client, user, TENANT_ORIGIN)

    assert not TENANT_CLAIMS & set(RefreshToken(pair["refresh"]).payload)


def _otp_login(client: Client, user: RevelUser, origin: str | None) -> dict[str, str]:
    headers = {"Origin": origin} if origin else {}
    with patch.object(auth_service, "verify_otp_jwt", return_value=(user, True)):
        response = client.post(
            reverse("api:token_obtain_pair_otp"),
            data=orjson.dumps({"token": "temp.jwt.token", "otp": "123456"}),
            content_type="application/json",
            headers=headers,
        )
    assert response.status_code == 200, response.content
    return t.cast(dict[str, str], response.json())


def test_otp_pair_on_tenant_origin_carries_claims(
    client: Client, totp_user: RevelUser, domain: OrganizationDomain
) -> None:
    pair = _otp_login(client, totp_user, TENANT_ORIGIN)

    for payload in (RefreshToken(pair["refresh"]).payload, AccessToken(pair["access"]).payload):
        assert payload["tenant_host"] == HOST
        assert isinstance(payload["tenant_session_iat"], int)


@pytest.mark.parametrize("origin", [None, PLATFORM_ORIGIN])
def test_otp_pair_without_tenant_origin_has_no_claims(
    client: Client, totp_user: RevelUser, domain: OrganizationDomain, origin: str | None
) -> None:
    pair = _otp_login(client, totp_user, origin)

    assert not TENANT_CLAIMS & set(RefreshToken(pair["refresh"]).payload)
    assert not TENANT_CLAIMS & set(AccessToken(pair["access"]).payload)


def test_other_callers_of_get_token_pair_stay_platform_only(user: RevelUser, domain: OrganizationDomain) -> None:
    pair = auth_service.get_token_pair_for_user(user)

    assert not TENANT_CLAIMS & set(RefreshToken(pair.refresh).payload)


# --- Refresh ---


def test_refresh_of_active_tenant_session_rotates_and_keeps_claims(
    client: Client, user: RevelUser, domain: OrganizationDomain
) -> None:
    old = _tenant_refresh(user)
    original_iat = old["tenant_session_iat"]

    response = _refresh(client, str(old))

    assert response.status_code == 200
    data = response.json()
    for payload in (RefreshToken(data["refresh"]).payload, AccessToken(data["access"]).payload):
        assert payload["tenant_host"] == HOST
        assert payload["tenant_session_iat"] == original_iat
    assert RefreshToken(data["refresh"])["jti"] != old["jti"]
    assert _blacklisted(old)
    assert _refresh(client, data["refresh"]).status_code == 200  # the rotated token keeps working


@pytest.mark.parametrize("origin", [TENANT_ORIGIN, PLATFORM_ORIGIN, "https://other.example.org", None])
def test_refresh_never_depends_on_origin_for_the_claims(
    client: Client, user: RevelUser, domain: OrganizationDomain, origin: str | None
) -> None:
    """The FE proxy (Origin = tenant) and any other origin neither strip nor add the claims."""
    tenant = _tenant_refresh(user)
    platform = RefreshToken.for_user(user)

    kept = _refresh(client, str(tenant), origin).json()
    added = _refresh(client, str(platform), origin).json()

    assert RefreshToken(kept["refresh"])["tenant_host"] == HOST
    assert AccessToken(kept["access"])["tenant_host"] == HOST
    assert not TENANT_CLAIMS & set(RefreshToken(added["refresh"]).payload)
    assert not TENANT_CLAIMS & set(AccessToken(added["access"]).payload)


@LIVE_STATE_REVOCATIONS
def test_refresh_is_refused_and_blacklisted_once_the_domain_stops_serving(
    client: Client, user: RevelUser, domain: OrganizationDomain, revoke: t.Callable[[OrganizationDomain], None]
) -> None:
    token = _tenant_refresh(user)
    assert _refresh(client, str(_tenant_refresh(user))).status_code == 200  # sanity: valid before

    revoke(domain)
    cache.clear()  # what the on_commit invalidation does in production
    response = _refresh(client, str(token), TENANT_ORIGIN)

    assert response.status_code == 401
    assert response.json()["code"] == "token_not_valid"
    assert _blacklisted(token)
    assert _refresh(client, str(token)).status_code == 401  # and it stays dead


def test_refresh_is_refused_for_a_session_older_than_the_reactivation(
    client: Client, user: RevelUser, domain: OrganizationDomain
) -> None:
    token = _tenant_refresh(user)
    custom_domain_service.mark_domain_failed(domain)
    _reactivate(domain)  # the hostname is serving again, but the session predates this verification
    cache.clear()

    response = _refresh(client, str(token))

    assert response.status_code == 401
    assert _blacklisted(token)
    assert _refresh(client, str(_tenant_refresh(user, issued_at=int(time.time())))).status_code == 200


@pytest.mark.parametrize(
    "extra", [{"tenant_session_iat": "yesterday"}, {"tenant_session_iat": True}, {"tenant_host": 7}]
)
def test_refresh_with_a_malformed_tenant_claim_is_refused(
    client: Client, user: RevelUser, domain: OrganizationDomain, extra: dict[str, t.Any]
) -> None:
    token = _tenant_refresh(user, **extra)

    assert _refresh(client, str(token)).status_code == 401
    assert _blacklisted(token)


def test_refresh_without_session_iat_is_refused(client: Client, user: RevelUser, domain: OrganizationDomain) -> None:
    token = RefreshToken.for_user(user)
    token.payload["tenant_host"] = HOST

    assert _refresh(client, str(token)).status_code == 401


def test_platform_refresh_is_unaffected_without_any_domain(client: Client, user: RevelUser) -> None:
    token = RefreshToken.for_user(user)

    response = _refresh(client, str(token), TENANT_ORIGIN)

    assert response.status_code == 200
    assert _blacklisted(token)  # normal rotation


def test_invalid_refresh_tokens_keep_the_stock_error(client: Client, user: RevelUser) -> None:
    assert _refresh(client, "garbage").status_code == 401
    spent = RefreshToken.for_user(user)
    assert _refresh(client, str(spent)).status_code == 200
    assert _refresh(client, str(spent)).status_code == 401  # rotated away already


# --- Access tokens ---


def _bearer(token: str) -> Client:
    return Client(HTTP_AUTHORIZATION=f"Bearer {token}")


def _tenant_access(user: RevelUser, **extra: t.Any) -> str:
    return str(_tenant_refresh(user, **extra).access_token)  # type: ignore[attr-defined]


def test_tenant_access_token_works_while_the_domain_is_active(user: RevelUser, domain: OrganizationDomain) -> None:
    assert _bearer(_tenant_access(user)).get(reverse("api:me")).status_code == 200


@LIVE_STATE_REVOCATIONS
def test_revoked_tenant_access_token_is_401_on_required_auth_and_anonymous_on_optional(
    user: RevelUser, domain: OrganizationDomain, revoke: t.Callable[[OrganizationDomain], None]
) -> None:
    client = _bearer(_tenant_access(user))
    revoke(domain)
    cache.clear()

    required = client.get(reverse("api:me"))
    optional = client.get(reverse("api:list_organizations"))

    assert required.status_code == 401
    assert required.json()["code"] == "token_not_valid"
    assert optional.status_code == 200


def test_optional_auth_treats_a_revoked_tenant_session_as_anonymous(
    user: RevelUser, domain: OrganizationDomain
) -> None:
    token = _tenant_access(user)
    custom_domain_service.remove_domain(domain)
    cache.clear()
    request = RequestFactory().get("/", HTTP_AUTHORIZATION=f"Bearer {token}")

    result = OptionalAuth()(request)

    assert isinstance(result, AnonymousUser)
    assert isinstance(request.user, AnonymousUser)


def test_optional_auth_still_rejects_expired_and_garbage_tokens(user: RevelUser) -> None:
    expired = AccessToken.for_user(user)
    expired.set_exp(lifetime=-timedelta(seconds=30))

    for token in (str(expired), "not-a-token"):
        request = RequestFactory().get("/", HTTP_AUTHORIZATION=f"Bearer {token}")
        with pytest.raises(InvalidToken) as excinfo:
            OptionalAuth()(request)
        assert not isinstance(excinfo.value, SessionRevoked)
        assert _bearer(token).get(reverse("api:list_organizations")).status_code == 401


def test_platform_access_tokens_are_unaffected(user: RevelUser) -> None:
    token = str(RefreshToken.for_user(user).access_token)  # type: ignore[attr-defined]
    client = _bearer(token)

    assert client.get(reverse("api:me")).status_code == 200
    assert client.get(reverse("api:list_organizations")).status_code == 200


def test_access_auth_leaves_the_reactivation_check_to_refresh(user: RevelUser, domain: OrganizationDomain) -> None:
    """Documented trade-off: the cached resolution has no ``verified_at``, so only refresh compares it."""
    custom_domain_service.mark_domain_failed(domain)
    _reactivate(domain)
    cache.clear()

    assert _bearer(_tenant_access(user)).get(reverse("api:me")).status_code == 200
    assert _refresh(Client(), str(_tenant_refresh(user))).status_code == 401


def test_access_token_without_session_iat_is_refused(user: RevelUser, domain: OrganizationDomain) -> None:
    token = RefreshToken.for_user(user)
    token.payload["tenant_host"] = HOST

    assert _bearer(str(token.access_token)).get(reverse("api:me")).status_code == 401  # type: ignore[attr-defined]


def test_scoped_auth_refuses_a_revoked_tenant_session_and_serves_platform_tokens(
    user: RevelUser, domain: OrganizationDomain
) -> None:
    """``ScopedJWTAuth`` shares the check; app tokens (no dots, no claim) never reach it."""
    tenant = _tenant_access(user)
    platform = str(RefreshToken.for_user(user).access_token)  # type: ignore[attr-defined]
    custom_domain_service.remove_domain(domain)
    cache.clear()

    with pytest.raises(SessionRevoked):
        ScopedJWTAuth().authenticate(RequestFactory().get("/"), tenant)
    assert ScopedJWTAuth().authenticate(RequestFactory().get("/"), platform) == user
    assert I18nJWTAuth().authenticate(RequestFactory().get("/"), platform) == user
