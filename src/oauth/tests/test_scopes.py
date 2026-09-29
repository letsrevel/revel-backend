"""Tests for the public scope registry and its DOT scopes backend."""

import typing as t

import pytest

from events.models import PermissionKey
from oauth.scopes import SCOPES, UNSCOPED_KEYS, RegistryScopes, scopes_for_key


def test_every_permission_key_is_mapped_or_explicitly_unscoped() -> None:
    keys = set(t.get_args(PermissionKey))
    mapped = {k for s in SCOPES.values() for k in s.permission_keys}
    assert mapped.isdisjoint(UNSCOPED_KEYS)
    assert mapped | UNSCOPED_KEYS == keys


def test_each_key_maps_to_exactly_one_scope() -> None:
    for key in t.get_args(PermissionKey):
        if key in UNSCOPED_KEYS:
            # The branch Task 5 keys on: an unscoped key unlocks nothing, so no scope can
            # grant it and no scope name can leak into a WWW-Authenticate challenge.
            assert scopes_for_key(key) == frozenset(), key
            continue
        assert len(scopes_for_key(key)) == 1, key


def test_backend_all_scopes_are_labelled() -> None:
    all_scopes = RegistryScopes().get_all_scopes()
    assert set(all_scopes) == set(SCOPES)
    assert all(isinstance(v, str) and v for v in all_scopes.values())


@pytest.mark.django_db
def test_backend_restricts_to_application_allowed_scopes(oauth_app: t.Any) -> None:
    oauth_app.allowed_scopes = ["org:read", "openid"]
    assert set(RegistryScopes().get_available_scopes(application=oauth_app)) == {"org:read", "openid"}
    assert set(RegistryScopes().get_available_scopes()) == set(SCOPES)
    assert RegistryScopes().get_default_scopes() == []

    oauth_app.allowed_scopes = []
    assert RegistryScopes().get_available_scopes(application=oauth_app) == []

    # A scope retired from the vocabulary must not stay grantable via a stale allowed_scopes row.
    oauth_app.allowed_scopes = ["org:read", "org:gone"]
    assert RegistryScopes().get_available_scopes(application=oauth_app) == ["org:read"]
