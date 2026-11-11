"""Link-token failures return a stable machine-readable ``code`` and never leak decoder internals."""

import datetime
import typing as t

import pytest
from django.conf import settings
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext

from accounts import schema
from accounts.exceptions import LinkTokenError, LinkTokenErrorCode
from accounts.jwt import create_token
from accounts.models import RevelUser
from accounts.service.account import token_to_payload

pytestmark = pytest.mark.django_db

LEAK_WORDS = ("Signature", "decode", "validation", "segments", "Not enough")


def _token(payload_cls: type[schema.BaseEmailJWTPayloadSchema], user: RevelUser, *, expired: bool = False) -> str:
    delta = datetime.timedelta(hours=-1 if expired else 1)
    payload = payload_cls(user_id=user.id, email=user.email, exp=timezone.now() + delta)
    return create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)


def _reset(client: Client, token: str) -> t.Any:
    body = {"token": token, "password1": "NewStr0ng!Passw0rd", "password2": "NewStr0ng!Passw0rd"}
    return client.post(reverse("api:reset-password"), data=body, content_type="application/json")


def _delete(client: Client, token: str) -> t.Any:
    return client.post(reverse("api:delete-account-confirm"), data={"token": token}, content_type="application/json")


def _verify(client: Client, token: str) -> t.Any:
    return client.post(reverse("api:verify-email"), data={"token": token}, content_type="application/json")


def _one_click(client: Client, token: str) -> t.Any:
    return client.post(reverse("api:one_click_unsubscribe") + f"?token={token}")


def _assert_invalid(response: t.Any) -> None:
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "invalid_token"
    assert body["detail"] == gettext("Invalid token.")
    assert not any(word.lower() in body["detail"].lower() for word in LEAK_WORDS)


def _assert_expired(response: t.Any) -> None:
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "token_expired"
    assert body["detail"] == gettext("Token has expired.")


def test_token_to_payload_raises_coded_errors(user: RevelUser) -> None:
    """The service raises LinkTokenError with the matching code."""
    with pytest.raises(LinkTokenError) as bad:
        token_to_payload("garbage", schema.PasswordResetJWTPayloadSchema)
    assert bad.value.code == LinkTokenErrorCode.INVALID_TOKEN
    assert str(bad.value) == gettext("Invalid token.")

    with pytest.raises(LinkTokenError) as expired:
        token_to_payload(
            _token(schema.PasswordResetJWTPayloadSchema, user, expired=True), schema.PasswordResetJWTPayloadSchema
        )
    assert expired.value.code == LinkTokenErrorCode.TOKEN_EXPIRED

    with pytest.raises(LinkTokenError) as wrong_type:
        token_to_payload(_token(schema.VerifyEmailJWTPayloadSchema, user), schema.PasswordResetJWTPayloadSchema)
    assert wrong_type.value.code == LinkTokenErrorCode.INVALID_TOKEN


def test_reset_password_token_codes(client: Client, user: RevelUser) -> None:
    """Password reset confirm: expired, garbage and wrong-type tokens."""
    _assert_expired(_reset(client, _token(schema.PasswordResetJWTPayloadSchema, user, expired=True)))
    _assert_invalid(_reset(client, "not.a.jwt"))
    tampered = _token(schema.PasswordResetJWTPayloadSchema, user)[:-4] + "AAAA"
    _assert_invalid(_reset(client, tampered))
    _assert_invalid(_reset(client, _token(schema.VerifyEmailJWTPayloadSchema, user)))


def test_delete_confirm_token_codes(client: Client, user: RevelUser) -> None:
    """Account delete confirm: expired, garbage and wrong-type tokens."""
    _assert_expired(_delete(client, _token(schema.DeleteAccountJWTPayloadSchema, user, expired=True)))
    _assert_invalid(_delete(client, "garbage"))
    _assert_invalid(_delete(client, _token(schema.PasswordResetJWTPayloadSchema, user)))


def test_verify_email_token_codes(client: Client, user: RevelUser) -> None:
    """Email verification: expired and garbage tokens."""
    _assert_expired(_verify(client, _token(schema.VerifyEmailJWTPayloadSchema, user, expired=True)))
    _assert_invalid(_verify(client, "garbage"))


def test_one_click_unsubscribe_bad_token_is_coded(client: Client) -> None:
    """The RFC 8058 POST still answers 400, now with a code."""
    _assert_invalid(_one_click(client, "garbage"))
