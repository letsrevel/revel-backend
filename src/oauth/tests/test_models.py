"""Tests for the swapped ``OAuthApplication`` model and its validation rules."""

import pytest
from django.core.exceptions import ValidationError

from accounts.models import RevelUser
from oauth.models import OAuthApplication

pytestmark = pytest.mark.django_db


def _app(user: RevelUser | None, **kwargs: object) -> OAuthApplication:
    defaults: dict[str, object] = {
        "user": user,
        "name": "X",
        "client_type": OAuthApplication.CLIENT_CONFIDENTIAL,
        "redirect_uris": "https://app.example/cb",
        "allowed_scopes": ["org:read"],
        "registration_source": OAuthApplication.RegistrationSource.MANUAL,
    }
    defaults.update(kwargs)
    return OAuthApplication(**defaults)


def test_defaults_are_rs256_and_authorization_code(user: RevelUser) -> None:
    app = _app(user)
    app.full_clean()
    assert app.algorithm == OAuthApplication.RS256_ALGORITHM
    assert app.authorization_grant_type == OAuthApplication.GRANT_AUTHORIZATION_CODE


def test_is_usable_follows_is_active(user: RevelUser) -> None:
    app = _app(user, is_active=False)
    assert app.is_usable(request=None) is False
    app.is_active = True
    assert app.is_usable(request=None) is True


def test_manual_app_requires_owner() -> None:
    with pytest.raises(ValidationError) as exc:
        _app(None).full_clean()
    assert "user" in exc.value.message_dict


def test_dynamic_app_must_not_have_owner(user: RevelUser) -> None:
    with pytest.raises(ValidationError) as exc:
        _app(user, registration_source=OAuthApplication.RegistrationSource.DCR).full_clean()
    assert "user" in exc.value.message_dict


def test_confidential_app_rejects_http_redirect(user: RevelUser) -> None:
    with pytest.raises(ValidationError) as exc:
        _app(user, redirect_uris="http://127.0.0.1/cb").full_clean()
    assert "redirect_uris" in exc.value.message_dict


def test_public_app_accepts_loopback_and_rejects_plain_http(user: RevelUser) -> None:
    for loopback in ("http://127.0.0.1:8123/cb", "http://[::1]:8123/cb"):
        _app(
            user,
            client_type=OAuthApplication.CLIENT_PUBLIC,
            client_secret="",
            redirect_uris=loopback,
        ).full_clean()
    with pytest.raises(ValidationError) as exc:
        _app(
            user,
            client_type=OAuthApplication.CLIENT_PUBLIC,
            client_secret="",
            redirect_uris="http://example.com/cb",
        ).full_clean()
    assert "redirect_uris" in exc.value.message_dict


def test_fragment_and_custom_scheme_rejected(user: RevelUser) -> None:
    """Both URIs are rejected, but today by DOT's AllowedURIValidator rather than by our ``clean()``.

    ``super().clean()`` runs first and rejects fragments and non-allowlisted schemes, so this
    pins the contract (a ``redirect_uris`` error) without pinning which layer enforces it.
    """
    for uri in ("https://app.example/cb#frag", "myapp://cb"):
        with pytest.raises(ValidationError) as exc:
            _app(user, redirect_uris=uri).full_clean()
        assert "redirect_uris" in exc.value.message_dict


def test_unknown_scope_rejected(user: RevelUser) -> None:
    with pytest.raises(ValidationError) as exc:
        _app(user, allowed_scopes=["org:nope"]).full_clean()
    assert "allowed_scopes" in exc.value.message_dict
