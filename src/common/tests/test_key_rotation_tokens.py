"""Every token family minted under the old SECRET_KEY keeps verifying while it is a fallback."""

import typing as t
import urllib.parse
from uuid import uuid4

import pyotp
import pytest
from ninja.errors import HttpError
from ninja_extra.exceptions import AuthenticationFailed

from accounts import schema as accounts_schema
from accounts.models import RevelUser
from accounts.service import account as account_service
from accounts.service import auth as auth_service
from accounts.service.account import token_to_payload
from common.signing import generate_signed_url, verify_signature
from events.service import guest as guest_service
from integrations.exceptions import IntegrationError
from integrations.service import state as integrations_state
from notifications.schema import OneClickUnsubscribePayload, UpdateNotificationPreferenceSchema
from notifications.service import unsubscribe as unsubscribe_service

pytestmark = pytest.mark.django_db

NEW = "brand-new-secret"


def _rotate(settings: t.Any, *, fallbacks: list[str]) -> None:
    settings.SECRET_KEY = NEW
    settings.SECRET_KEY_FALLBACKS = fallbacks


def _verify_email(token: str) -> None:
    token_to_payload(token, accounts_schema.VerifyEmailJWTPayloadSchema)


def _otp(user: RevelUser) -> t.Callable[[str], object]:
    def verify(token: str) -> None:
        _user, verified = auth_service.verify_otp_jwt(token, pyotp.TOTP(user.totp_secret).now())
        assert verified

    return verify


def _signed_url_ok(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    qs = urllib.parse.parse_qs(parsed.query)
    assert verify_signature(parsed.path, qs["exp"][0], qs["sig"][0])


def _case(name: str, user: RevelUser) -> tuple[str, t.Callable[[str], object], type[Exception]]:
    """Return (token minted under the current key, verifier, exception raised on rejection)."""
    if name == "verify_email":
        return account_service.create_verification_token(user), _verify_email, HttpError
    if name == "otp":
        return auth_service.get_temporary_otp_jwt(user), _otp(user), AuthenticationFailed
    if name == "guest":
        return (
            guest_service.create_guest_rsvp_token(user, uuid4(), "yes"),
            guest_service.validate_and_decode_guest_token,
            HttpError,
        )
    if name == "integrations_state":
        return (
            integrations_state.mint_state(organization_id=uuid4(), user_id=user.id, provider="eventbrite"),
            integrations_state.validate_state,
            IntegrationError,
        )
    raise AssertionError(name)


FAMILIES = ["verify_email", "otp", "guest", "integrations_state"]


@pytest.mark.parametrize("family", FAMILIES)
def test_old_key_accepted_while_fallback_then_rejected(family: str, user: RevelUser, settings: t.Any) -> None:
    old = settings.SECRET_KEY
    token, verify, error = _case(family, user)
    _rotate(settings, fallbacks=[old])
    verify(token)  # does not raise
    _rotate(settings, fallbacks=[])
    with pytest.raises(error) as exc:
        verify(token)
    if type(exc.value) is HttpError:
        assert exc.value.status_code == 400  # signature failure, not e.g. the blacklist's 401


def test_signed_file_url(settings: t.Any) -> None:
    old = settings.SECRET_KEY
    url = generate_signed_url("protected/a.pdf")
    _rotate(settings, fallbacks=[old])
    _signed_url_ok(url)
    new_url = generate_signed_url("protected/a.pdf")
    assert new_url != url  # signs with the primary key
    _signed_url_ok(new_url)
    _rotate(settings, fallbacks=[])
    with pytest.raises(AssertionError):
        _signed_url_ok(url)


def test_unsubscribe_links_survive_with_dedicated_keys(user: RevelUser, settings: t.Any) -> None:
    old = settings.SECRET_KEY
    token = unsubscribe_service.generate_unsubscribe_token(user)
    # Rotate with NO SECRET_KEY fallback, pinning the old key as an unsubscribe key.
    settings.SECRET_KEY = NEW
    settings.SECRET_KEY_FALLBACKS = []
    settings.UNSUBSCRIBE_SIGNING_KEYS = ["unsub-new", old]
    prefs = unsubscribe_service.confirm_unsubscribe(
        token, UpdateNotificationPreferenceSchema(enabled_channels=["in_app"])
    )
    assert prefs.user_id == user.id
    # Newly minted links use the first unsubscribe key, so they don't verify against the main keys.
    fresh = unsubscribe_service.generate_unsubscribe_token(user)
    with pytest.raises(HttpError):
        token_to_payload(fresh, OneClickUnsubscribePayload)


def test_old_secret_signed_non_unsubscribe_token_rejected_by_auth_paths(user: RevelUser, settings: t.Any) -> None:
    old = settings.SECRET_KEY
    token = account_service.create_verification_token(user)
    settings.SECRET_KEY = NEW
    settings.SECRET_KEY_FALLBACKS = []
    settings.UNSUBSCRIBE_SIGNING_KEYS = ["unsub-new", old]
    with pytest.raises(HttpError):
        _verify_email(token)
