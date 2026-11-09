"""Refresh-token rotation: recording, single-use reuse grace and lineage logout (through the HTTP endpoints).

True concurrency is serialized by the user-row lock plus the atomic ``cache.add``; these tests pin the
sequential behaviour that follows from it.
"""

import contextlib
import time
import typing as t
from datetime import timedelta
from unittest.mock import MagicMock, patch

import jwt
import orjson
import pytest
from django.core.cache import cache
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from ninja_jwt.tokens import RefreshToken

from accounts.jwt import blacklist_user_tokens
from accounts.models import RevelUser
from accounts.service import account as account_service
from accounts.service import global_ban_service
from events.models import Organization, OrganizationDomain
from events.service import custom_domain_service

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _grace(settings: t.Any) -> None:
    settings.REFRESH_TOKEN_REUSE_GRACE_SECONDS = 30


def _login(user: RevelUser) -> str:
    """A fresh login's refresh token (what token/pair mints): outstanding, no ``sid``."""
    return str(RefreshToken.for_user(user))


def _post(client: Client, name: str, refresh: str) -> t.Any:
    return client.post(reverse(f"api:{name}"), data=orjson.dumps({"refresh": refresh}), content_type="application/json")


def _refresh(client: Client, refresh: str) -> t.Any:
    return _post(client, "token_refresh", refresh)


def _ok(client: Client, refresh: str) -> dict[str, str]:
    response = _refresh(client, refresh)
    assert response.status_code == 200, response.content
    return t.cast(dict[str, str], response.json())


def _jti(raw: str) -> str:
    return str(jwt.decode(raw, options={"verify_signature": False})["jti"])


def _is_blacklisted(raw: str) -> bool:
    return BlacklistedToken.objects.filter(token__jti=_jti(raw)).exists()


def _rows() -> tuple[int, int]:
    return OutstandingToken.objects.count(), BlacklistedToken.objects.count()


def test_normal_refresh_records_the_rotated_token(client: Client, user: RevelUser) -> None:
    r1 = _login(user)
    body = _ok(client, r1)
    r2 = body["refresh"]

    assert _is_blacklisted(r1)
    row = OutstandingToken.objects.get(jti=_jti(r2))
    assert row.user == user
    assert jwt.decode(r2, options={"verify_signature": False})["sid"] == _jti(r1)
    # Returned as an instance: no double rotation, so the returned refresh token is itself usable.
    assert not _is_blacklisted(r2)
    _ok(client, r2)
    response = client.get(reverse("api:me"), headers={"Authorization": f"Bearer {body['access']}"})
    assert response.status_code == 200


def test_reuse_within_grace_issues_a_new_pair_once(client: Client, user: RevelUser) -> None:
    r1 = _login(user)
    r2 = _ok(client, r1)["refresh"]

    r3 = _ok(client, r1)["refresh"]
    assert r3 != r2
    assert jwt.decode(r3, options={"verify_signature": False})["sid"] == _jti(r1)

    again = _refresh(client, r1)
    assert again.status_code == 401
    assert again.json()["code"] == "token_not_valid"
    assert again.json()["detail"] == "Token is blacklisted"
    _ok(client, r3)
    _ok(client, r2)  # the original successor is not invalidated by the grace


def test_reuse_after_grace_expired_is_rejected(client: Client, user: RevelUser) -> None:
    r1 = _login(user)
    _ok(client, r1)
    cache.delete(f"refresh_rotated:{_jti(r1)}")
    assert _refresh(client, r1).status_code == 401


def test_grace_zero_disables_reuse_and_writes_no_marker(client: Client, user: RevelUser, settings: t.Any) -> None:
    settings.REFRESH_TOKEN_REUSE_GRACE_SECONDS = 0
    r1 = _login(user)
    _ok(client, r1)
    assert cache.get(f"refresh_rotated:{_jti(r1)}") is None
    assert _refresh(client, r1).status_code == 401


def test_blacklist_without_rotation_gets_no_grace(client: Client, user: RevelUser) -> None:
    token = RefreshToken.for_user(user)
    token.blacklist()  # type: ignore[attr-defined]
    assert _refresh(client, str(token)).status_code == 401


def test_blacklist_user_tokens_kills_rotated_sessions(client: Client, user: RevelUser) -> None:
    r1 = _login(user)
    r2 = _ok(client, r1)["refresh"]
    blacklist_user_tokens(user)
    assert _refresh(client, r1).status_code == 401  # no grace after a revocation
    assert _refresh(client, r2).status_code == 401  # the rotated token was recorded, so it was revoked


def test_logout_kills_the_whole_lineage(client: Client, user: RevelUser) -> None:
    r1 = _login(user)
    r2 = _ok(client, r1)["refresh"]
    r3 = _ok(client, r1)["refresh"]

    response = _post(client, "token_logout", r3)
    assert response.status_code == 204
    assert response.content == b""
    for raw in (r1, r2, r3):
        assert _refresh(client, raw).status_code == 401


def test_logout_with_a_stale_token_kills_live_successors(client: Client, user: RevelUser) -> None:
    r1 = _login(user)
    r2 = _ok(client, r1)["refresh"]
    assert _post(client, "token_logout", r1).status_code == 204
    assert _refresh(client, r2).status_code == 401


def test_logout_leaves_other_logins_alone(client: Client, user: RevelUser) -> None:
    mine, other = _login(user), _login(user)
    _post(client, "token_logout", mine)
    _ok(client, other)


def test_inactive_user_gets_no_grace(client: Client, user: RevelUser) -> None:
    r1 = _login(user)
    _ok(client, r1)
    RevelUser.objects.filter(pk=user.pk).update(is_active=False)
    assert _refresh(client, r1).status_code == 401


@contextlib.contextmanager
def _cache_failing(method: str) -> t.Iterator[None]:
    """Make one cache method raise for the refresh service only (the throttles share the real cache)."""
    fake = MagicMock(wraps=cache)
    getattr(fake, method).side_effect = RuntimeError("redis down")
    with patch("accounts.service.token_refresh.cache", fake):
        yield


def test_cache_errors_reject_reuse_but_never_break_refresh(client: Client, user: RevelUser) -> None:
    r1 = _login(user)
    with _cache_failing("set"):
        r2 = _ok(client, r1)["refresh"]
    # No marker was written, so the reuse is rejected.
    assert _refresh(client, r1).status_code == 401

    r3 = _login(user)
    _ok(client, r3)
    with _cache_failing("get"):
        assert _refresh(client, r3).status_code == 401
    r4 = _login(user)
    _ok(client, r4)
    with _cache_failing("add"):
        assert _refresh(client, r4).status_code == 401
    _ok(client, r2)


def test_logout_with_garbage_is_401(client: Client) -> None:
    assert _post(client, "token_logout", "not-a-token").status_code == 401


def test_rejected_reuse_has_no_db_side_effects(client: Client, user: RevelUser) -> None:
    r1 = _login(user)
    _ok(client, r1)
    cache.delete(f"refresh_rotated:{_jti(r1)}")
    before = _rows()
    assert _refresh(client, r1).status_code == 401
    assert _rows() == before


def test_password_reset_kills_rotated_sessions_and_grace(client: Client, user: RevelUser) -> None:
    r1 = _login(user)
    r2 = _ok(client, r1)["refresh"]
    account_service.reset_password(account_service.create_password_reset_token(user), "a-Strong-password-123!")
    assert _refresh(client, r1).status_code == 401
    assert _refresh(client, r2).status_code == 401


def test_global_ban_kills_rotated_sessions_and_grace(client: Client, user: RevelUser) -> None:
    r1 = _login(user)
    r2 = _ok(client, r1)["refresh"]
    global_ban_service.deactivate_user_for_ban(user, "test")
    user.refresh_from_db()
    assert not user.is_active
    assert _refresh(client, r1).status_code == 401
    assert _refresh(client, r2).status_code == 401


def test_logout_with_an_expired_token_is_401(client: Client, user: RevelUser) -> None:
    token = RefreshToken.for_user(user)
    token.set_exp(from_time=timezone.now() - timedelta(days=60), lifetime=timedelta(days=1))
    assert _post(client, "token_logout", str(token)).status_code == 401


def test_token_rotated_before_the_deploy_still_rotates_and_gets_grace(client: Client, user: RevelUser) -> None:
    """A legacy token has no ``sid`` and (if rotated pre-deploy) no outstanding row of its own."""
    token = RefreshToken.for_user(user)
    r1 = str(token)
    OutstandingToken.objects.filter(jti=_jti(r1)).delete()

    r2 = _ok(client, r1)["refresh"]
    r3 = _ok(client, r1)["refresh"]  # grace: lineage is live through the new user-owned row
    assert r3 != r2
    assert _refresh(client, r1).status_code == 401


# --- Tenant-bound session ---

HOST = "events.example.com"


@pytest.fixture
def domain(user: RevelUser) -> OrganizationDomain:
    org = Organization.objects.create(name="Tenant Org", slug="tenant-org", owner=user, custom_domain_enabled=True)
    row = OrganizationDomain.objects.create(organization=org, hostname=HOST, cloudflare_hostname_id="cf-1")
    custom_domain_service.activate_domain(row)
    OrganizationDomain.objects.filter(pk=row.pk).update(verified_at=timezone.now() - timedelta(hours=1))
    cache.clear()
    return row


def test_tenant_revoke_gives_no_grace(client: Client, user: RevelUser, domain: OrganizationDomain) -> None:
    token = RefreshToken.for_user(user)
    token.payload.update({"tenant_host": HOST, "tenant_session_iat": int(time.time()) - 1800})
    r1 = str(token)
    r2 = _ok(client, r1)["refresh"]

    custom_domain_service.remove_domain(domain)
    cache.clear()  # what the on_commit invalidation does in production
    cache.set(f"refresh_rotated:{_jti(r1)}", 1, timeout=30)  # the marker is still inside its window

    assert _refresh(client, r1).status_code == 401
    assert _is_blacklisted(r1)
    assert _refresh(client, r2).status_code == 401
