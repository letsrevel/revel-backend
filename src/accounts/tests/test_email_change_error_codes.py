"""Email-change confirm refusals return a stable machine-readable ``code`` alongside the translated detail."""

import typing as t
from unittest.mock import patch

import orjson
import pytest
from django.conf import settings
from django.db import IntegrityError
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext

from accounts import schema
from accounts.exceptions import EmailChangeError, EmailChangeErrorCode
from accounts.jwt import create_token
from accounts.models import RevelUser
from accounts.service import account as account_service

pytestmark = pytest.mark.django_db


def _make_change_token(user: RevelUser, new_email: str) -> str:
    payload = schema.EmailChangeJWTPayloadSchema(
        user_id=user.id,
        email=user.email,
        new_email=new_email,
        exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME,
        iat=timezone.now(),
    )
    return create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)


def _confirm(client: Client, token: str) -> t.Any:
    return client.post(
        reverse("api:email-change-confirm"),
        data=orjson.dumps({"token": token}),
        content_type="application/json",
    )


def test_email_taken_returns_code(client: Client, user: RevelUser, django_user_model: t.Type[RevelUser]) -> None:
    """Confirming onto an address another account already holds is a coded 400."""
    token = _make_change_token(user, "taken@example.com")
    django_user_model.objects.create_user(username="taken@example.com", email="taken@example.com", password="x")

    response = _confirm(client, token)

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == EmailChangeErrorCode.EMAIL_TAKEN.value == "email_taken"
    assert body["detail"] == gettext("This email is already in use.")


def test_email_taken_code_is_stable_in_another_locale(
    client: Client, user: RevelUser, django_user_model: t.Type[RevelUser]
) -> None:
    """The ``code`` stays the same while the ``detail`` follows the request locale (the reason the code exists)."""
    from django.utils import translation

    token = _make_change_token(user, "taken-de@example.com")
    django_user_model.objects.create_user(username="taken-de@example.com", email="taken-de@example.com", password="x")
    english = "This email is already in use."
    with translation.override("de"):
        german = gettext(english)
    assert german != english  # the German catalog must translate it

    response = client.post(
        reverse("api:email-change-confirm"),
        data=orjson.dumps({"token": token}),
        content_type="application/json",
        HTTP_ACCEPT_LANGUAGE="de",
    )

    assert response.status_code == 400
    assert response.json() == {"detail": german, "code": "email_taken"}


def test_integrity_race_returns_email_taken_code(
    client: Client, user: RevelUser, django_user_model: t.Type[RevelUser]
) -> None:
    """The pre-check passes but the save loses the race to a concurrent confirmation."""
    token = _make_change_token(user, "raced@example.com")

    with patch.object(RevelUser, "save", side_effect=IntegrityError("duplicate")):
        response = _confirm(client, token)

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "email_taken"
    assert body["detail"] == gettext("This email is already in use.")
    user.refresh_from_db()
    assert user.email != "raced@example.com"


def test_superseded_by_password_reset_returns_code(client: Client, user: RevelUser) -> None:
    """A change link issued before a password reset is a coded 400."""
    token = _make_change_token(user, "intruder@example.com")
    account_service.reset_password(account_service.create_password_reset_token(user), "a-new-valid-Password-123!")

    response = _confirm(client, token)

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "email_change_superseded"
    assert body["detail"] == gettext("This email change link is no longer valid. Please request a new one.")


def test_service_raises_typed_error_for_taken_email(user: RevelUser, django_user_model: t.Type[RevelUser]) -> None:
    """The service raises :class:`EmailChangeError` carrying the machine code."""
    token = _make_change_token(user, "svc-taken@example.com")
    django_user_model.objects.create_user(username="svc-taken@example.com", email="svc-taken@example.com", password="x")

    with pytest.raises(EmailChangeError) as exc:
        account_service.confirm_email_change(token)
    assert exc.value.code == EmailChangeErrorCode.EMAIL_TAKEN
