"""Tests for ``RevelOAuth2Validator``: scope-gated OIDC claims and real ``offline_access``."""

import typing as t
from types import SimpleNamespace

import pytest
from oauth2_provider.models import AccessToken, RefreshToken

from accounts.models import RevelUser
from oauth.models import OAuthApplication
from oauth.validator import RevelOAuth2Validator

pytestmark = pytest.mark.django_db

# Thumbnails are derived from the source path, so they too live under ``protected/``
# (``common/thumbnails/service.py``). The file need not exist to build its URL.
THUMBNAIL_NAME = "protected/profile-pictures/u/avatar_thumbnail.jpg"


def _request(user: RevelUser, scopes: list[str]) -> t.Any:
    """A minimal stand-in for the oauthlib request DOT hands the validator.

    ``validate_bearer_token`` sets ``request.scopes = list(access_token.scopes)`` and the
    grant types set it from ``scope_to_list``, so a list of scope names is the real shape.
    """
    return SimpleNamespace(user=user, scopes=scopes, grant_type="authorization_code")


def _token_request(user: RevelUser, app: OAuthApplication, scopes: list[str]) -> t.Any:
    """A request shaped for the *real* ``_save_bearer_token`` path.

    ``authorization_code`` because the ``offline_access`` gate only applies to issuing grants
    (a refresh always rotates); ``code=None`` makes DOT's ``Grant`` lookup for resource
    narrowing find nothing rather than needing a row.
    """
    return SimpleNamespace(user=user, scopes=scopes, client=app, grant_type="authorization_code", code=None)


def test_profile_and_email_claims(user: RevelUser) -> None:
    user.preferred_name = "Bee"
    user.language = "it"
    user.save()
    claims = RevelOAuth2Validator().get_additional_claims(_request(user, ["openid", "profile", "email"]))
    assert claims["sub"] == str(user.pk)
    assert claims["name"] == "Bee"
    assert claims["locale"] == "it"
    assert claims["email"] == user.email
    # ``email_verified`` is False for the root factory's user and must survive the
    # ``is not None`` filter rather than being dropped as falsy.
    assert claims["email_verified"] is False
    assert "picture" not in claims  # no picture uploaded


def test_name_claim_falls_back_to_the_full_name(user: RevelUser) -> None:
    """``preferred_name`` then ``get_full_name()`` — the first two of the display name's three
    components, without its ``username`` fallback (R-56).
    """
    user.preferred_name = ""
    user.first_name = "Ada"
    user.last_name = "Lovelace"
    user.save()
    claims = RevelOAuth2Validator().get_additional_claims(_request(user, ["openid", "profile"]))
    assert claims["name"] == user.get_full_name() == "Ada Lovelace"


def test_profile_scope_never_discloses_the_email(user: RevelUser) -> None:
    """``username`` IS the email on every creation path here, so no ``profile`` claim may
    carry it: the separate ``email`` scope is the only thing the user consented to for that
    (R-56). The all-blank name case is the one that leaked, via ``get_display_name()``'s
    ``username`` fallback.
    """
    user.preferred_name = ""
    user.first_name = ""
    user.last_name = ""
    user.profile_picture_thumbnail = THUMBNAIL_NAME
    user.save()
    assert user.username == user.email  # the premise: the product has no separate username

    claims = RevelOAuth2Validator().get_additional_claims(_request(user, ["openid", "profile"]))
    leaking = {key: value for key, value in claims.items() if isinstance(value, str) and user.email in value}
    assert not leaking


def test_name_claim_is_omitted_when_the_user_has_no_name(user: RevelUser) -> None:
    """Better no ``name`` than the email as a name."""
    user.preferred_name = ""
    user.first_name = ""
    user.last_name = ""
    user.save()
    claims = RevelOAuth2Validator().get_additional_claims(_request(user, ["openid", "profile"]))
    assert "name" not in claims


def test_no_scope_no_claim(user: RevelUser) -> None:
    claims = RevelOAuth2Validator().get_additional_claims(_request(user, ["openid"]))
    assert set(claims) == {"sub"}


def test_never_leaks_staff_flags(superuser: RevelUser) -> None:
    claims = RevelOAuth2Validator().get_additional_claims(_request(superuser, ["openid", "profile", "email"]))
    assert not {"is_staff", "is_superuser", "groups"} & set(claims)


def test_picture_claim_is_absolute_and_signed(user: RevelUser) -> None:
    """A protected thumbnail needs a signed, absolute URL to be fetchable by the app."""
    user.profile_picture_thumbnail = THUMBNAIL_NAME
    user.save()
    claims = RevelOAuth2Validator().get_additional_claims(_request(user, ["openid", "profile"]))
    picture: str = claims["picture"]
    assert picture.startswith("http://testserver/")  # the ``oauth_provider`` fixture's issuer
    assert "sig=" in picture and "exp=" in picture


def test_email_claims_omitted_when_the_user_has_no_email(user: RevelUser) -> None:
    """An empty ``email`` would pass the ``None`` filter and tell the app nothing."""
    user.email = ""
    user.save()
    claims = RevelOAuth2Validator().get_additional_claims(_request(user, ["openid", "email"]))
    assert set(claims) == {"sub"}


def test_claim_scope_map_is_frozen() -> None:
    """The two sync tests below derive their expectation FROM the map, so a claim added to
    both map and builder would pass unnoticed. This literal makes every future claim a
    deliberate edit to a test — and every claim leaves the system to a third-party app.
    """
    assert RevelOAuth2Validator.oidc_claim_scope == {
        "sub": "openid",
        "name": "profile",
        "picture": "profile",
        "locale": "profile",
        "email": "email",
        "email_verified": "email",
    }


def test_claim_scope_map_matches_emitted_claims(user: RevelUser) -> None:
    """``oidc_claim_scope`` and the emitted dict must agree key for key, both ways.

    DOT's ``get_oidc_claims`` drops any claim whose key is missing from the map, and
    advertising a key that is never emitted lies to clients.
    """
    user.profile_picture_thumbnail = THUMBNAIL_NAME
    user.save()
    validator = RevelOAuth2Validator()
    claims = validator.get_additional_claims(_request(user, ["openid", "profile", "email"]))
    assert set(claims) == set(validator.oidc_claim_scope)


@pytest.mark.parametrize("scope", ["profile", "email"])
def test_each_scope_emits_exactly_the_claims_it_advertises(user: RevelUser, scope: str) -> None:
    user.profile_picture_thumbnail = THUMBNAIL_NAME
    user.save()
    validator = RevelOAuth2Validator()
    claims = validator.get_additional_claims(_request(user, ["openid", scope]))
    expected = {key for key, required in validator.oidc_claim_scope.items() if required in {"openid", scope}}
    assert set(claims) == expected


def test_refresh_token_dropped_without_offline_access(user: RevelUser, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, t.Any] = {}
    monkeypatch.setattr(
        "oauth2_provider.oauth2_validators.OAuth2Validator._save_bearer_token",
        lambda self, token, request, *a, **k: captured.update(token),
    )
    token = {"access_token": "a", "refresh_token": "r", "scope": "openid"}
    RevelOAuth2Validator().save_bearer_token(token, _request(user, ["openid"]))
    assert "refresh_token" not in token and "refresh_token" not in captured


def test_refresh_token_kept_with_offline_access(user: RevelUser, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "oauth2_provider.oauth2_validators.OAuth2Validator._save_bearer_token",
        lambda self, token, request, *a, **k: None,
    )
    token = {"access_token": "a", "refresh_token": "r", "scope": "openid offline_access"}
    RevelOAuth2Validator().save_bearer_token(token, _request(user, ["openid", "offline_access"]))
    assert token["refresh_token"] == "r"


def test_no_refresh_token_row_is_persisted_without_offline_access(user: RevelUser, oauth_app: OAuthApplication) -> None:
    """The real DOT path, not the monkeypatched seam: nothing may reach the database."""
    token = {"access_token": "a", "refresh_token": "r", "scope": "openid"}
    RevelOAuth2Validator().save_bearer_token(token, _token_request(user, oauth_app, ["openid"]))
    assert AccessToken.objects.count() == 1
    assert not RefreshToken.objects.exists()


def test_refresh_token_row_is_persisted_with_offline_access(user: RevelUser, oauth_app: OAuthApplication) -> None:
    token = {"access_token": "a", "refresh_token": "r", "scope": "openid offline_access"}
    RevelOAuth2Validator().save_bearer_token(token, _token_request(user, oauth_app, ["openid", "offline_access"]))
    assert AccessToken.objects.count() == 1
    assert RefreshToken.objects.count() == 1
    # A right-count/wrong-shape row must not pass: the refresh token has to belong to the
    # access token this call just minted.
    assert RefreshToken.objects.get().access_token == AccessToken.objects.get()
