"""Tests for the my-permissions payload cache (#880).

Pins the contract of ``events.service.permission_snapshot``: short-TTL per-user cache,
post-commit (never in-transaction) invalidation at the two grant sites, rollback safety,
and fail-open behavior when the cache backend errors.
"""

import typing as t

import pytest
from django.core.cache import cache
from django.db import transaction
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Organization, OrganizationStaff, OrganizationToken
from events.service import permission_snapshot
from events.service.organization_service import lifecycle, tokens

pytestmark = pytest.mark.django_db


@pytest.fixture
def user(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="snapshot-user", email="snapshot@example.com", password="p", email_verified=True
    )


def test_payload_is_cached_and_reused(user: RevelUser) -> None:
    """The second call is served from the cache: later DB changes are not reflected."""
    first = permission_snapshot.get_my_permissions_payload(user)
    assert first["organization_permissions"] == {}

    org = Organization.objects.create(name="Fresh Org", owner=RevelUser.objects.create_user("other-owner"))
    OrganizationStaff.objects.create(organization=org, user=user)

    second = permission_snapshot.get_my_permissions_payload(user)
    assert second == first  # stale by design (60s TTL) — served from cache

    cache.delete(permission_snapshot.get_cache_key(user.id))
    third = permission_snapshot.get_my_permissions_payload(user)
    assert str(org.id) in third["organization_permissions"]


def test_cached_payload_matches_fresh_build(member_client: Client, member_user: RevelUser) -> None:
    """First (miss) and second (hit) responses are byte-identical through the endpoint."""
    url = reverse("api:my_permissions")
    fresh = member_client.get(url)
    cached = member_client.get(url)
    assert fresh.status_code == cached.status_code == 200
    assert fresh.json() == cached.json()


def test_create_organization_invalidates_on_commit(user: RevelUser, django_capture_on_commit_callbacks: t.Any) -> None:
    """The grant-site delete is deferred to commit — never issued inside the transaction."""
    key = permission_snapshot.get_cache_key(user.id)
    permission_snapshot.get_my_permissions_payload(user)  # populate
    assert cache.get(key) is not None

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        lifecycle.create_organization(owner=user, name="Snapshot Org", contact_email=user.email)
        # Still cached: an in-transaction delete would reopen the repopulate race (#880).
        assert cache.get(key) is not None

    for callback in callbacks:
        callback()
    assert cache.get(key) is None

    payload = permission_snapshot.get_my_permissions_payload(user)
    assert "owner" in payload["organization_permissions"].values()


def test_claim_invitation_invalidates_on_commit(
    user: RevelUser, organization: Organization, django_capture_on_commit_callbacks: t.Any
) -> None:
    """Claiming a staff-granting token busts the claimer's cached map after commit."""
    issuer = RevelUser.objects.create_user("token-issuer")
    token = OrganizationToken.objects.create(
        organization=organization, issuer=issuer, grants_staff_status=True, grants_membership=False
    )
    key = permission_snapshot.get_cache_key(user.id)
    permission_snapshot.get_my_permissions_payload(user)
    assert cache.get(key) is not None

    with django_capture_on_commit_callbacks(execute=True):
        claimed = tokens.claim_invitation(user, str(token.pk))
    assert claimed == organization
    assert cache.get(key) is None

    payload = permission_snapshot.get_my_permissions_payload(user)
    assert str(organization.id) in payload["organization_permissions"]


def test_rollback_leaves_cache_untouched(user: RevelUser) -> None:
    """A rolled-back mutation discards the on_commit delete — the cached value is still true."""
    key = permission_snapshot.get_cache_key(user.id)
    permission_snapshot.get_my_permissions_payload(user)

    class Boom(Exception):
        pass

    with pytest.raises(Boom), transaction.atomic():
        permission_snapshot.invalidate_my_permissions(user.id)
        raise Boom()

    assert cache.get(key) is not None


class _BrokenCache:
    """Stand-in for a down Redis: every operation raises.

    Swapped in for the *module binding* in ``permission_snapshot`` only — patching
    methods on the shared Django cache proxy would also break the global throttles,
    which 500 the request before it ever reaches the endpoint.
    """

    def get(self, *args: t.Any, **kwargs: t.Any) -> t.Any:
        raise ConnectionError("redis down")

    def set(self, *args: t.Any, **kwargs: t.Any) -> t.Any:
        raise ConnectionError("redis down")

    def delete_many(self, *args: t.Any, **kwargs: t.Any) -> t.Any:
        raise ConnectionError("redis down")


def test_endpoint_fails_open_when_cache_errors(
    member_client: Client, member_user: RevelUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken cache backend degrades to the DB build — never a 500."""
    monkeypatch.setattr(permission_snapshot, "cache", _BrokenCache())

    response = member_client.get(reverse("api:my_permissions"))
    assert response.status_code == 200
    assert str(Organization.objects.get(members=member_user).id) in response.json()["memberships"]


def test_invalidation_delete_failure_does_not_raise(
    user: RevelUser, monkeypatch: pytest.MonkeyPatch, django_capture_on_commit_callbacks: t.Any
) -> None:
    """A failed cache delete is swallowed — the mutation must never fail on Redis."""
    monkeypatch.setattr(permission_snapshot, "cache", _BrokenCache())
    with django_capture_on_commit_callbacks(execute=True):
        permission_snapshot.invalidate_my_permissions(user.id)  # must not raise


def test_cache_key_is_versioned() -> None:
    """The key embeds the bumpable version constant (rolling-deploy shape safety)."""
    assert f":{permission_snapshot.CACHE_VERSION}:" in permission_snapshot.get_cache_key("abc")
