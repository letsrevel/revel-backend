"""Tests for the one-time OIDC login hand-off token."""

import time
import typing as t
from datetime import timedelta
from uuid import uuid4

import jwt as pyjwt
import pytest
from django.conf import settings
from ninja_extra.exceptions import AuthenticationFailed

from accounts.jwt import create_oidc_login_token, validate_oidc_login_token


def test_round_trip() -> None:
    user_id = str(uuid4())
    token = create_oidc_login_token(user_id=user_id, return_url="/events/x", jti="abc")
    payload = validate_oidc_login_token(token)
    assert str(payload.user_id) == user_id
    assert payload.return_url == "/events/x"
    assert payload.jti == "abc"
    assert payload.type == "oidc-login"


def test_expired_token_rejected(settings: t.Any) -> None:
    settings.OIDC_LOGIN_TOKEN_LIFETIME = timedelta(seconds=-1)
    token = create_oidc_login_token(user_id=str(uuid4()), return_url="/", jti="x")
    with pytest.raises(AuthenticationFailed, match="expired"):
        validate_oidc_login_token(token)


def test_wrong_type_rejected() -> None:
    now = int(time.time())
    forged = pyjwt.encode(
        {
            "iss": "https://api.letsrevel.io/",
            "aud": settings.JWT_AUDIENCE,
            "jti": "x",
            "exp": now + 60,
            "iat": now,
            "type": "impersonation-request",
            "user_id": str(uuid4()),
            "return_url": "/",
        },
        settings.SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    with pytest.raises(AuthenticationFailed, match="type"):
        validate_oidc_login_token(forged)


def test_bad_signature_rejected() -> None:
    token = create_oidc_login_token(user_id=str(uuid4()), return_url="/", jti="x")
    with pytest.raises(AuthenticationFailed):
        validate_oidc_login_token(token[:-2] + "zz")
