"""Tests for linking/creating a Revel user from verified OIDC claims."""

import typing as t
from unittest.mock import patch

import orjson
import pytest
from django.db.models import Q
from django.test.client import Client
from django.urls import reverse

from accounts.exceptions import OIDCLoginError
from accounts.models import ExternalIdentity, GlobalBan, RevelUser
from accounts.service import account as account_service
from accounts.service import oidc
from accounts.service.oidc import OIDCClaims
from common.utils import get_or_create_with_race_protection
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


def test_new_user_unverified_claim_refused() -> None:
    """An unverified IdP address must not mint an account Revel then treats as email-verified."""
    with pytest.raises(OIDCLoginError) as exc:
        oidc._resolve_user(GOOGLE, claims(email_verified=False))
    assert exc.value.code == "unverified_email"
    assert not RevelUser.objects.filter(username="alice@example.com").exists()
    assert not ExternalIdentity.objects.exists()


def test_lost_create_race_still_applies_existing_user_checks(inactive_user: RevelUser) -> None:
    """If another request creates the account between lookup and insert, the race helper hands back
    that row with ``created=False``; it must go through the same checks as a pre-existing account."""
    with (
        patch("accounts.service.oidc.get_or_create_with_race_protection", side_effect=[(inactive_user, False)]),
        pytest.raises(OIDCLoginError) as exc,
    ):
        oidc._resolve_user(GOOGLE, claims())
    assert exc.value.code == "inactive"


def test_new_user_email_is_lowercased() -> None:
    user = oidc._resolve_user(GOOGLE, claims(email="John.Doe@Example.com"))
    assert user.username == user.email == "john.doe@example.com"
    assert user.external_identities.get().email == "john.doe@example.com"


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


def test_identity_create_race_converges(user: RevelUser) -> None:
    """Two concurrent first-time links for the same (provider, subject) must converge on one row.

    ``_resolve_user`` links via ``get_or_create_with_race_protection(ExternalIdentity, ...)`` for
    exactly this reason: a plain ``ExternalIdentity.objects.create(...)`` would let the losing
    request of a concurrent pair hit the ``uniq_external_identity_provider_subject`` constraint
    as an unhandled ``IntegrityError`` instead of converging on the winner's row. This directly
    exercises the helper with the same lookup/defaults ``_resolve_user`` uses, simulating the
    "loser" call finding the row the "winner" already committed.
    """
    lookup = Q(provider="google", subject="sub-1")
    defaults = {"user": user, "provider": "google", "subject": "sub-1", "email": user.email}

    first, first_created = get_or_create_with_race_protection(ExternalIdentity, lookup, defaults)
    second, second_created = get_or_create_with_race_protection(ExternalIdentity, lookup, defaults)

    assert (first_created, second_created) == (True, False)
    assert first == second
    assert ExternalIdentity.objects.filter(provider="google", subject="sub-1").count() == 1


def test_password_login_works_with_linked_identity(client: Client, user: RevelUser) -> None:
    ExternalIdentity.objects.create(user=user, provider="google", subject="1")
    response = client.post(
        reverse("api:token_obtain_pair"),
        data=orjson.dumps({"username": user.username, "password": "strong-password-123!"}),
        content_type="application/json",
    )
    assert response.status_code == 200
    assert "access" in response.json()


@patch("accounts.tasks.send_account_email.delay")
def test_password_reset_request_allowed_with_linked_identity(mock_send: t.Any, user: RevelUser) -> None:
    ExternalIdentity.objects.create(user=user, provider="google", subject="1")
    assert account_service.request_password_reset(user.email) is not None


@patch("accounts.tasks.send_account_email.delay")
def test_email_change_allowed_with_linked_identity(mock_send: t.Any, user: RevelUser) -> None:
    ExternalIdentity.objects.create(user=user, provider="google", subject="1")
    token = account_service.request_email_change(
        user=user, new_email="new@example.com", password="strong-password-123!"
    )
    assert isinstance(token, str) and token
