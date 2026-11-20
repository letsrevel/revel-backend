"""Per-account TOTP failure lock (#1429) across login step 2 and OTP enable/disable."""

import typing as t
from unittest.mock import MagicMock, patch

import orjson
import pyotp
import pytest
from django.core.cache import cache
from django.test.client import Client
from django.urls import reverse
from django.utils import translation
from ninja_jwt.token_blacklist.models import BlacklistedToken
from ninja_jwt.tokens import RefreshToken

from accounts.exceptions import LoginErrorCode
from accounts.jwt import validate_otp_jwt
from accounts.models import RevelUser
from accounts.service import auth as auth_service
from accounts.service import totp as totp_service
from accounts.tasks import AccountEmail
from api.api import api

pytestmark = pytest.mark.django_db

PASSWORD = "strong-password-123!"
WRONG = "000000"
LOCKED_MESSAGE = (
    "Too many incorrect two-factor codes. For your security, verification is paused. Please try again later."
)


@pytest.fixture(autouse=True)
def _throttling_off(settings: t.Any) -> None:
    """Prove the lock does not depend on the rate throttle: throttling stays off for these tests."""
    settings.DISABLE_THROTTLING = True


@pytest.fixture
def mock_email() -> t.Generator[MagicMock]:
    with patch("accounts.tasks.send_account_email.delay") as delay:
        yield delay


def _post(client: Client, name: str, data: dict[str, str]) -> t.Any:
    return client.post(reverse(f"api:{name}"), data=orjson.dumps(data), content_type="application/json")


def _otp_step(client: Client, user: RevelUser, otp: str) -> t.Any:
    """Run step 2 with a fresh temp token, as a client has to after every refused attempt."""
    token = auth_service.get_temporary_otp_jwt(user)
    return _post(client, "token_obtain_pair_otp", {"token": token, "otp": otp})


def _lock_via_login(client: Client, user: RevelUser) -> None:
    for _ in range(totp_service.FAILURE_THRESHOLD - 1):
        assert _otp_step(client, user, WRONG).status_code == 401
    assert _otp_step(client, user, WRONG).status_code == 429


# --- login step 2 ---


def test_fifth_wrong_code_answers_429_with_code_and_retry_after(client: Client, totp_user: RevelUser) -> None:
    for _ in range(totp_service.FAILURE_THRESHOLD - 1):
        assert _otp_step(client, totp_user, WRONG).status_code == 401

    response = _otp_step(client, totp_user, WRONG)

    assert response.status_code == 429
    assert response.json() == {"detail": LOCKED_MESSAGE, "code": "totp_locked"}
    assert 1 <= int(response["Retry-After"]) <= totp_service.LOCK_SECONDS


def test_correct_code_is_refused_once_locked(client: Client, totp_user: RevelUser, mock_email: MagicMock) -> None:
    _lock_via_login(client, totp_user)

    response = _otp_step(client, totp_user, pyotp.TOTP(totp_user.totp_secret).now())

    assert response.status_code == 429
    assert response.json()["code"] == "totp_locked"
    assert "access" not in response.json()


def test_locked_response_keeps_the_temp_token_blacklisted_and_the_email_pending(
    client: Client, totp_user: RevelUser, mock_email: MagicMock, django_capture_on_commit_callbacks: t.Any
) -> None:
    """The 429 must not roll back the burned temp token or the lock email.

    ``verify_otp_jwt`` used to wrap the whole check in ``@transaction.atomic``: raising out of that
    savepoint discarded both the blacklist row and the ``on_commit`` email callback.
    """
    for _ in range(totp_service.FAILURE_THRESHOLD - 1):
        _otp_step(client, totp_user, WRONG)
    token = auth_service.get_temporary_otp_jwt(totp_user)
    jti = validate_otp_jwt(token).jti

    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        response = _post(client, "token_obtain_pair_otp", {"token": token, "otp": WRONG})

    assert response.status_code == 429
    assert BlacklistedToken.objects.filter(token__jti=jti).exists()
    assert len(callbacks) == 1
    mock_email.assert_called_once()
    assert mock_email.call_args.args == (AccountEmail.TOTP_LOCKED, totp_user.email)


def test_step_one_still_answers_while_locked(client: Client, totp_user: RevelUser, mock_email: MagicMock) -> None:
    """No oracle before the password: step 1 is unchanged and just hands out a temp token."""
    _lock_via_login(client, totp_user)

    response = _post(client, "token_obtain_pair", {"username": totp_user.username, "password": PASSWORD})

    assert response.status_code == 200
    assert response.json()["type"] == "otp"


def test_wrong_password_is_unchanged_while_locked(client: Client, totp_user: RevelUser, mock_email: MagicMock) -> None:
    _lock_via_login(client, totp_user)

    response = _post(client, "token_obtain_pair", {"username": totp_user.username, "password": "wrong"})

    assert response.status_code == 401


def test_success_before_the_threshold_resets_the_counter(client: Client, totp_user: RevelUser) -> None:
    for _ in range(totp_service.FAILURE_THRESHOLD - 1):
        assert _otp_step(client, totp_user, WRONG).status_code == 401
    assert _otp_step(client, totp_user, pyotp.TOTP(totp_user.totp_secret).now()).status_code == 200

    for _ in range(totp_service.FAILURE_THRESHOLD - 1):
        assert _otp_step(client, totp_user, WRONG).status_code == 401


def test_lock_ends_and_login_works_again(client: Client, totp_user: RevelUser, mock_email: MagicMock) -> None:
    _lock_via_login(client, totp_user)

    cache.delete(totp_service._lock_key(totp_user))

    assert _otp_step(client, totp_user, pyotp.TOTP(totp_user.totp_secret).now()).status_code == 200


def test_locked_message_follows_the_request_language(
    client: Client, totp_user: RevelUser, mock_email: MagicMock
) -> None:
    _lock_via_login(client, totp_user)
    token = auth_service.get_temporary_otp_jwt(totp_user)

    with translation.override("de"):
        response = client.post(
            reverse("api:token_obtain_pair_otp"),
            data=orjson.dumps({"token": token, "otp": WRONG}),
            content_type="application/json",
            headers={"Accept-Language": "de"},
        )

    assert response.status_code == 429
    assert response.json()["code"] == "totp_locked"
    assert response.json()["detail"] == (
        "Zu viele falsche Zwei-Faktor-Codes. Zu deiner Sicherheit ist die Verifizierung pausiert. "
        "Bitte versuche es später erneut."
    )


def test_temp_token_cannot_be_redeemed_twice(client: Client, totp_user: RevelUser) -> None:
    token = auth_service.get_temporary_otp_jwt(totp_user)
    code = pyotp.TOTP(totp_user.totp_secret).now()
    assert _post(client, "token_obtain_pair_otp", {"token": token, "otp": code}).status_code == 200

    assert _post(client, "token_obtain_pair_otp", {"token": token, "otp": code}).status_code == 401


# --- OTP enable / disable ---


def test_enable_wrong_code_is_still_403(auth_client: Client, user: RevelUser) -> None:
    response = _post(auth_client, "enable-otp", {"otp": WRONG})

    assert response.status_code == 403
    assert "Invalid OTP" in response.json()["detail"]


def test_enable_locks_after_five_wrong_codes(auth_client: Client, user: RevelUser, mock_email: MagicMock) -> None:
    for _ in range(totp_service.FAILURE_THRESHOLD - 1):
        assert _post(auth_client, "enable-otp", {"otp": WRONG}).status_code == 403

    response = _post(auth_client, "enable-otp", {"otp": WRONG})

    assert response.status_code == 429
    assert response.json() == {"detail": LOCKED_MESSAGE, "code": "totp_locked"}
    assert "Retry-After" in response
    user.refresh_from_db()
    assert user.totp_active is False


def test_enable_refuses_the_correct_code_while_locked(
    auth_client: Client, user: RevelUser, mock_email: MagicMock
) -> None:
    for _ in range(totp_service.FAILURE_THRESHOLD):
        _post(auth_client, "enable-otp", {"otp": WRONG})

    response = _post(auth_client, "enable-otp", {"otp": pyotp.TOTP(user.totp_secret).now()})

    assert response.status_code == 429
    user.refresh_from_db()
    assert user.totp_active is False


def test_disable_locks_after_five_wrong_codes_and_keeps_2fa_on(
    auth_client: Client, user: RevelUser, mock_email: MagicMock
) -> None:
    RevelUser.objects.filter(pk=user.pk).update(totp_active=True)
    for _ in range(totp_service.FAILURE_THRESHOLD - 1):
        assert _post(auth_client, "disable-otp", {"otp": WRONG}).status_code == 403

    response = _post(auth_client, "disable-otp", {"otp": WRONG})
    correct = _post(auth_client, "disable-otp", {"otp": pyotp.TOTP(user.totp_secret).now()})

    assert response.status_code == 429
    assert correct.status_code == 429
    user.refresh_from_db()
    assert user.totp_active is True


def test_login_and_disable_share_one_counter(client: Client, totp_user: RevelUser, mock_email: MagicMock) -> None:
    """Failures on the login step and on the disable endpoint add up for the same account."""
    other = Client(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(totp_user).access_token}")  # type: ignore[attr-defined]
    for _ in range(3):
        assert _otp_step(client, totp_user, WRONG).status_code == 401
    assert _post(other, "disable-otp", {"otp": WRONG}).status_code == 403

    assert _post(other, "disable-otp", {"otp": WRONG}).status_code == 429
    assert _otp_step(client, totp_user, pyotp.TOTP(totp_user.totp_secret).now()).status_code == 429


# --- contract ---


def test_login_error_code_set_includes_totp_locked() -> None:
    assert {c.value for c in LoginErrorCode} == {"email_not_verified", "totp_locked"}


def test_openapi_documents_429_on_the_three_otp_routes() -> None:
    api._schema_cache = {}
    schema = api.get_openapi_schema()
    for path in ("/api/auth/token/pair/otp", "/api/otp/verify", "/api/otp/disable"):
        responses = schema["paths"][path]["post"]["responses"]
        assert 429 in responses, path
    assert 429 not in schema["paths"]["/api/auth/token/pair"]["post"]["responses"]


def test_openapi_429_code_is_optional_because_the_rate_limit_has_none() -> None:
    """A throttle 429 carries no ``code``; only the account lock does. The generated types must say so."""
    api._schema_cache = {}
    schema = api.get_openapi_schema()
    for path in ("/api/auth/token/pair/otp", "/api/otp/verify", "/api/otp/disable"):
        ref = schema["paths"][path]["post"]["responses"][429]["content"]["application/json"]["schema"]["$ref"]
        body = schema["components"]["schemas"][ref.rsplit("/", 1)[1]]
        assert "code" in body["properties"], path
        assert "code" not in body.get("required", []), path
        assert "detail" in body["required"], path
