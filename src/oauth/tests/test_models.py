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


def test_non_authorization_code_grant_rejected(user: RevelUser) -> None:
    """R-57: v1 ships only the authorization-code grant, enforced at the model boundary.

    A client-credentials token has no user, which makes the OIDC claim builder fail at
    ``/userinfo``; admin and dynamic registration both go through ``full_clean()``.
    """
    for grant in (
        OAuthApplication.GRANT_CLIENT_CREDENTIALS,
        OAuthApplication.GRANT_PASSWORD,
        OAuthApplication.GRANT_IMPLICIT,
        OAuthApplication.GRANT_DEVICE_CODE,
    ):
        with pytest.raises(ValidationError) as exc:
            _app(user, authorization_grant_type=grant).full_clean()
        assert "authorization_grant_type" in exc.value.message_dict, grant


def test_refresh_token_is_not_a_separate_grant_type() -> None:
    """The R-57 rule cannot break refresh: DOT has no ``refresh_token`` grant choice."""
    assert "refresh_token" not in dict(OAuthApplication.GRANT_TYPES)


def test_dynamic_app_gets_the_whole_scope_vocabulary(user: RevelUser) -> None:
    """spec §6.1: ``allowed_scopes`` = every registry scope for a dynamic client."""
    from oauth.scopes import SCOPES

    app = _app(None, registration_source=OAuthApplication.RegistrationSource.DCR, allowed_scopes=[])
    app.save()
    app.refresh_from_db()
    assert set(app.allowed_scopes) == set(SCOPES)

    manual = _app(user, allowed_scopes=[])
    manual.save()
    manual.refresh_from_db()
    assert manual.allowed_scopes == []


def test_revoking_a_dynamic_apps_scopes_is_not_re_seeded() -> None:
    """The seed is INSERT-only: on update, empty means revoked, not "not set yet".

    Re-seeding on every save would invert the scope-shrink path (and the admin) from a full
    revocation into the widest possible grant.
    """
    app = _app(None, registration_source=OAuthApplication.RegistrationSource.DCR, allowed_scopes=[])
    app.save()
    assert app.allowed_scopes  # seeded on insert

    app.allowed_scopes = []
    app.save()
    app.refresh_from_db()
    assert app.allowed_scopes == []

    app.allowed_scopes = []
    app.save(update_fields=["allowed_scopes"])
    app.refresh_from_db()
    assert app.allowed_scopes == []


def test_save_does_not_write_fields_update_fields_did_not_name() -> None:
    """A save that names only ``name`` must not also write ``allowed_scopes``."""
    app = _app(None, registration_source=OAuthApplication.RegistrationSource.DCR, allowed_scopes=[])
    app.save()
    OAuthApplication.objects.filter(pk=app.pk).update(allowed_scopes=[])

    app.name = "Renamed"
    app.allowed_scopes = []
    app.save(update_fields=["name"])
    app.refresh_from_db()
    assert app.name == "Renamed"
    assert app.allowed_scopes == []
