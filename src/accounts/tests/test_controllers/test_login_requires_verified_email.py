"""Password login, its 2FA step and refresh rotation refuse a non-guest account whose email is unverified."""

import typing as t

import orjson
import pyotp
import pytest
from django.test.client import Client
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from accounts.service import auth as auth_service

pytestmark = pytest.mark.django_db

PASSWORD = "strong-password-123!"  # noqa: S105 - test fixture password


@pytest.fixture
def unverified(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="unverified@example.com", email="unverified@example.com", password=PASSWORD, email_verified=False
    )


def _post(client: Client, name: str, payload: dict[str, t.Any]) -> t.Any:
    return client.post(reverse(f"api:{name}"), data=orjson.dumps(payload), content_type="application/json")


def test_unverified_account_with_correct_password_gets_403_with_code(client: Client, unverified: RevelUser) -> None:
    response = _post(client, "token_obtain_pair", {"username": unverified.username, "password": PASSWORD})

    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "email_not_verified"
    assert body["detail"]
    assert "access" not in body


def test_unverified_account_with_wrong_password_gets_the_normal_refusal(client: Client, unverified: RevelUser) -> None:
    wrong = _post(client, "token_obtain_pair", {"username": unverified.username, "password": "wrong-password-1!"})
    unknown = _post(client, "token_obtain_pair", {"username": "nobody@example.com", "password": "wrong-password-1!"})

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_unverified_account_with_totp_is_refused_before_the_second_step(client: Client, unverified: RevelUser) -> None:
    RevelUser.objects.filter(pk=unverified.pk).update(totp_active=True)

    response = _post(client, "token_obtain_pair", {"username": unverified.username, "password": PASSWORD})

    assert response.status_code == 403
    assert response.json()["code"] == "email_not_verified"


def test_verified_account_gets_a_token_pair(client: Client, unverified: RevelUser) -> None:
    RevelUser.objects.filter(pk=unverified.pk).update(email_verified=True)

    response = _post(client, "token_obtain_pair", {"username": unverified.username, "password": PASSWORD})

    assert response.status_code == 200
    assert response.json()["access"]


def test_otp_step_refuses_an_unverified_account(client: Client, unverified: RevelUser) -> None:
    RevelUser.objects.filter(pk=unverified.pk).update(totp_active=True)
    unverified.refresh_from_db()
    temp = auth_service.get_temporary_otp_jwt(unverified)
    code = pyotp.TOTP(unverified.totp_secret).now()

    response = _post(client, "token_obtain_pair_otp", {"token": temp, "otp": code})

    assert response.status_code == 403
    assert response.json()["code"] == "email_not_verified"


def test_otp_step_with_a_wrong_code_still_answers_401(client: Client, unverified: RevelUser) -> None:
    RevelUser.objects.filter(pk=unverified.pk).update(totp_active=True)
    unverified.refresh_from_db()
    temp = auth_service.get_temporary_otp_jwt(unverified)

    response = _post(client, "token_obtain_pair_otp", {"token": temp, "otp": "000000"})

    assert response.status_code == 401


def test_refresh_of_an_unverified_users_token_is_refused_and_burned(client: Client, unverified: RevelUser) -> None:
    refresh = str(RefreshToken.for_user(unverified))

    first = _post(client, "token_refresh", {"refresh": refresh})
    RevelUser.objects.filter(pk=unverified.pk).update(email_verified=True)
    second = _post(client, "token_refresh", {"refresh": refresh})

    assert first.status_code == 401
    assert second.status_code == 401  # the refused token was blacklisted


def test_refresh_of_a_deleted_users_token_is_refused(client: Client, unverified: RevelUser) -> None:
    refresh = str(RefreshToken.for_user(unverified))
    RevelUser.objects.filter(pk=unverified.pk).delete()

    assert _post(client, "token_refresh", {"refresh": refresh}).status_code == 401


def test_refresh_of_a_verified_users_token_rotates(client: Client, unverified: RevelUser) -> None:
    RevelUser.objects.filter(pk=unverified.pk).update(email_verified=True)

    response = _post(client, "token_refresh", {"refresh": str(RefreshToken.for_user(unverified))})

    assert response.status_code == 200


def test_refresh_of_a_guests_token_still_rotates(client: Client, guest_user: RevelUser) -> None:
    assert guest_user.email_verified is False

    response = _post(client, "token_refresh", {"refresh": str(RefreshToken.for_user(guest_user))})

    assert response.status_code == 200


def test_guest_is_not_subject_to_the_requirement(guest_user: RevelUser) -> None:
    auth_service.require_verified_email(guest_user)
    assert auth_service.is_session_allowed(guest_user)
