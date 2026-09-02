"""Tests for linking/creating a Revel user from verified OIDC claims."""

import typing as t

import pytest

from accounts.exceptions import OIDCLoginError
from accounts.models import ExternalIdentity, GlobalBan, RevelUser
from accounts.service import oidc
from accounts.service.oidc import OIDCClaims
from revel.oidc_config import OIDCProviderConfig

pytestmark = pytest.mark.django_db

GOOGLE = OIDCProviderConfig(
    key="google", name="Google", issuer="https://accounts.google.com", client_id="c", client_secret="s"
)


def claims(**overrides: t.Any) -> OIDCClaims:
    base: dict[str, t.Any] = {
        "sub": "sub-1",
        "email": "alice@example.com",
        "email_verified": True,
        "given_name": "Alice",
        "family_name": "Doe",
        "locale": "de-AT",
    }
    return OIDCClaims(**{**base, **overrides})


def test_existing_identity_logs_in(user: RevelUser) -> None:
    ExternalIdentity.objects.create(user=user, provider="google", subject="sub-1")
    assert oidc._resolve_user(GOOGLE, claims(email="different@example.com")) == user
    assert user.external_identities.count() == 1


def test_existing_identity_inactive_user(inactive_user: RevelUser) -> None:
    ExternalIdentity.objects.create(user=inactive_user, provider="google", subject="sub-1")
    with pytest.raises(OIDCLoginError) as exc:
        oidc._resolve_user(GOOGLE, claims())
    assert exc.value.code == "inactive"


def test_existing_user_verified_email_gets_linked(user: RevelUser) -> None:
    result = oidc._resolve_user(GOOGLE, claims(email=user.email))
    assert result == user
    identity = user.external_identities.get()
    assert (identity.provider, identity.subject, identity.email) == ("google", "sub-1", user.email)
    user.refresh_from_db()
    assert user.check_password("strong-password-123!")  # password untouched


def test_existing_user_case_insensitive_match(user: RevelUser) -> None:
    assert oidc._resolve_user(GOOGLE, claims(email=user.email.upper())) == user


def test_existing_user_unverified_claim_refused(user: RevelUser) -> None:
    with pytest.raises(OIDCLoginError) as exc:
        oidc._resolve_user(GOOGLE, claims(email=user.email, email_verified=False))
    assert exc.value.code == "unverified_email"
    assert not ExternalIdentity.objects.exists()


def test_existing_unverified_user_becomes_verified(unverified_user: RevelUser) -> None:
    oidc._resolve_user(GOOGLE, claims(email=unverified_user.email))
    unverified_user.refresh_from_db()
    assert unverified_user.email_verified is True


def test_guest_user_is_upgraded(guest_user: RevelUser) -> None:
    result = oidc._resolve_user(GOOGLE, claims(email=guest_user.email))
    result.refresh_from_db()
    assert result == guest_user
    assert (result.guest, result.email_verified, result.is_active) == (False, True, True)
    assert result.external_identities.count() == 1


def test_inactive_non_guest_refused(inactive_user: RevelUser) -> None:
    with pytest.raises(OIDCLoginError) as exc:
        oidc._resolve_user(GOOGLE, claims(email=inactive_user.email))
    assert exc.value.code == "inactive"


def test_new_user_created() -> None:
    user = oidc._resolve_user(GOOGLE, claims())
    assert user.username == "alice@example.com"
    assert user.email == "alice@example.com"
    assert (user.first_name, user.last_name) == ("Alice", "Doe")
    assert (user.email_verified, user.guest, user.is_active) == (True, False, True)
    assert user.language == "de"
    assert not user.has_usable_password()
    assert (user.is_staff, user.is_superuser) == (False, False)
    assert user.external_identities.get().subject == "sub-1"


@pytest.mark.parametrize(("locale", "expected"), [(None, "en"), ("xx-YY", "en"), ("it", "it"), ("fr-CH", "fr")])
def test_language_from_locale(locale: str | None, expected: str) -> None:
    assert oidc._language_from_locale(locale) == expected


def test_no_email_claim_refused() -> None:
    with pytest.raises(OIDCLoginError) as exc:
        oidc._resolve_user(GOOGLE, claims(email=None))
    assert exc.value.code == "no_email"


def test_banned_email_refused(superuser: RevelUser) -> None:
    GlobalBan.objects.create(ban_type=GlobalBan.BanType.EMAIL, value="alice@example.com", created_by=superuser)
    with pytest.raises(OIDCLoginError) as exc:
        oidc._resolve_user(GOOGLE, claims())
    assert exc.value.code == "banned"
    assert not RevelUser.objects.filter(email="alice@example.com").exists()


def test_banned_email_refused_even_with_identity(user: RevelUser, superuser: RevelUser) -> None:
    ExternalIdentity.objects.create(user=user, provider="google", subject="sub-1")
    GlobalBan.objects.create(ban_type=GlobalBan.BanType.EMAIL, value=user.email, created_by=superuser)
    with pytest.raises(OIDCLoginError) as exc:
        oidc._resolve_user(GOOGLE, claims())
    assert exc.value.code == "banned"
