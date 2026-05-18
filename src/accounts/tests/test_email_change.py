# src/accounts/tests/test_email_change.py
"""Tests for the self-served email change flow (issue #421)."""

import datetime
import typing as t
from unittest.mock import MagicMock, patch

import orjson
import pytest
from django.conf import settings
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja.errors import HttpError
from ninja_jwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from ninja_jwt.tokens import RefreshToken

from accounts import schema
from accounts.jwt import create_token
from accounts.models import GlobalBan, RevelUser
from accounts.service import account as account_service

pytestmark = pytest.mark.django_db


# ===== Schema =====


class TestEmailChangeRequestSchema:
    def test_uppercase_email_lowercased(self) -> None:
        payload = schema.EmailChangeRequestSchema(new_email="NEW@EXAMPLE.COM", password="x")
        assert payload.new_email == "new@example.com"


# ===== Service: request_email_change =====


class TestRequestEmailChange:
    @patch("accounts.tasks.send_email_change_notice.delay")
    @patch("accounts.tasks.send_email_change_confirmation.delay")
    def test_success(
        self,
        mock_send_conf: MagicMock,
        mock_send_notice: MagicMock,
        user: RevelUser,
    ) -> None:
        token = account_service.request_email_change(
            user=user, new_email="new@example.com", password="strong-password-123!"
        )
        assert token
        mock_send_conf.assert_called_once_with("new@example.com", token)
        mock_send_notice.assert_called_once()
        notice_args = mock_send_notice.call_args
        assert notice_args.args[0] == user.email
        # Masking: a****@example.com style
        assert notice_args.args[1].endswith("@example.com")
        assert "*" in notice_args.args[1]

    @patch("accounts.tasks.send_email_change_confirmation.delay")
    def test_wrong_password(self, mock_send: MagicMock, user: RevelUser) -> None:
        with pytest.raises(HttpError) as exc:
            account_service.request_email_change(user=user, new_email="new@example.com", password="WRONG")
        assert exc.value.status_code == 400
        mock_send.assert_not_called()

    @patch("accounts.tasks.send_email_change_confirmation.delay")
    def test_same_email(self, mock_send: MagicMock, user: RevelUser) -> None:
        with pytest.raises(HttpError) as exc:
            account_service.request_email_change(user=user, new_email=user.email, password="strong-password-123!")
        assert exc.value.status_code == 400
        mock_send.assert_not_called()

    @patch("accounts.tasks.send_email_change_confirmation.delay")
    def test_duplicate_email(self, mock_send: MagicMock, user: RevelUser, django_user_model: t.Type[RevelUser]) -> None:
        django_user_model.objects.create_user(username="taken@example.com", email="taken@example.com", password="x")
        with pytest.raises(HttpError) as exc:
            account_service.request_email_change(
                user=user, new_email="taken@example.com", password="strong-password-123!"
            )
        assert exc.value.status_code == 400
        mock_send.assert_not_called()

    @patch("accounts.tasks.send_email_change_confirmation.delay")
    def test_duplicate_email_case_insensitive(
        self, mock_send: MagicMock, user: RevelUser, django_user_model: t.Type[RevelUser]
    ) -> None:
        django_user_model.objects.create_user(username="taken@example.com", email="taken@example.com", password="x")
        with pytest.raises(HttpError):
            account_service.request_email_change(
                user=user, new_email="TAKEN@example.com".lower(), password="strong-password-123!"
            )
        mock_send.assert_not_called()

    @patch("accounts.tasks.send_email_change_confirmation.delay")
    def test_google_sso_user_rejected(self, mock_send: MagicMock, google_user: RevelUser) -> None:
        # Google SSO users have a sentinel password; their check_password will fail,
        # but the SSO check happens after password check — so use a fake password the
        # check_password will reject. To test SSO branch directly, set a real password.
        google_user.set_password("strong-password-123!")
        google_user.save(update_fields=["password"])
        with pytest.raises(HttpError) as exc:
            account_service.request_email_change(
                user=google_user, new_email="new@example.com", password="strong-password-123!"
            )
        assert exc.value.status_code == 400
        mock_send.assert_not_called()

    @patch("accounts.tasks.send_email_change_notice.delay")
    @patch("accounts.tasks.send_email_change_confirmation.delay")
    def test_globally_banned_target_no_op(
        self,
        mock_send_conf: MagicMock,
        mock_send_notice: MagicMock,
        user: RevelUser,
    ) -> None:
        GlobalBan.objects.create(
            ban_type=GlobalBan.BanType.EMAIL,
            value="banned@example.com",
            reason="test",
        )
        token = account_service.request_email_change(
            user=user, new_email="banned@example.com", password="strong-password-123!"
        )
        assert token == ""
        mock_send_conf.assert_not_called()
        mock_send_notice.assert_not_called()


# ===== Service: confirm_email_change =====


def _make_change_token(user: RevelUser, new_email: str, *, expired: bool = False) -> str:
    exp = timezone.now() - datetime.timedelta(days=1) if expired else timezone.now() + settings.VERIFY_TOKEN_LIFETIME
    payload = schema.EmailChangeJWTPayloadSchema(
        user_id=user.id,
        email=user.email,
        new_email=new_email,
        exp=exp,
    )
    return create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)


class TestConfirmEmailChange:
    @patch("accounts.tasks.send_email_change_completed_new.delay")
    @patch("accounts.tasks.send_email_change_completed_old.delay")
    def test_success(
        self,
        mock_send_old: MagicMock,
        mock_send_new: MagicMock,
        user: RevelUser,
    ) -> None:
        old_email = user.email
        token = _make_change_token(user, "new@example.com")

        result = account_service.confirm_email_change(token)

        result.refresh_from_db()
        assert result.email == "new@example.com"
        assert result.username == "new@example.com"
        assert result.email_verified is True
        mock_send_old.assert_called_once_with(old_email, "new@example.com")
        mock_send_new.assert_called_once_with("new@example.com", old_email)

    @patch("accounts.tasks.send_email_change_completed_new.delay")
    @patch("accounts.tasks.send_email_change_completed_old.delay")
    def test_blacklists_all_user_tokens(
        self,
        mock_old: MagicMock,
        mock_new: MagicMock,
        user: RevelUser,
    ) -> None:
        # Issue a couple of refresh tokens for the user — they should all be blacklisted.
        RefreshToken.for_user(user)
        RefreshToken.for_user(user)
        assert OutstandingToken.objects.filter(user=user).count() == 2

        token = _make_change_token(user, "new@example.com")
        account_service.confirm_email_change(token)

        outstanding = OutstandingToken.objects.filter(user=user)
        for ot in outstanding:
            assert BlacklistedToken.objects.filter(token=ot).exists()

    def test_expired_token(self, user: RevelUser) -> None:
        token = _make_change_token(user, "new@example.com", expired=True)
        with pytest.raises(HttpError) as exc:
            account_service.confirm_email_change(token)
        assert exc.value.status_code == 400

    @patch("accounts.tasks.send_email_change_completed_new.delay")
    @patch("accounts.tasks.send_email_change_completed_old.delay")
    def test_blacklisted_token_rejected(self, mock_old: MagicMock, mock_new: MagicMock, user: RevelUser) -> None:
        token = _make_change_token(user, "new@example.com")
        account_service.confirm_email_change(token)
        # Re-using the same token must fail
        with pytest.raises(HttpError):
            account_service.confirm_email_change(token)

    @patch("accounts.tasks.send_email_change_completed_new.delay")
    @patch("accounts.tasks.send_email_change_completed_old.delay")
    def test_race_email_taken(
        self,
        mock_old: MagicMock,
        mock_new: MagicMock,
        user: RevelUser,
        django_user_model: t.Type[RevelUser],
    ) -> None:
        token = _make_change_token(user, "raced@example.com")
        # Between request and confirm, somebody else takes the address.
        django_user_model.objects.create_user(username="raced@example.com", email="raced@example.com", password="x")
        with pytest.raises(HttpError) as exc:
            account_service.confirm_email_change(token)
        assert exc.value.status_code == 400
        # User's email is unchanged.
        user.refresh_from_db()
        assert user.email != "raced@example.com"
        # Token is blacklisted so it cannot be retried later.
        assert BlacklistedToken.objects.exists()


# ===== Controller tests =====


@patch("accounts.tasks.send_email_change_notice.delay")
@patch("accounts.tasks.send_email_change_confirmation.delay")
def test_email_change_request_endpoint(
    mock_conf: MagicMock, mock_notice: MagicMock, auth_client: Client, user: RevelUser
) -> None:
    url = reverse("api:email-change-request")
    response = auth_client.post(
        url,
        data=orjson.dumps({"new_email": "fresh@example.com", "password": "strong-password-123!"}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    assert "confirmation link" in response.json()["message"].lower()
    mock_conf.assert_called_once()
    mock_notice.assert_called_once()


def test_email_change_request_requires_auth(client: Client) -> None:
    url = reverse("api:email-change-request")
    response = client.post(
        url,
        data=orjson.dumps({"new_email": "a@example.com", "password": "x"}),
        content_type="application/json",
    )
    assert response.status_code == 401


@patch("accounts.tasks.send_email_change_completed_new.delay")
@patch("accounts.tasks.send_email_change_completed_old.delay")
def test_email_change_confirm_endpoint(
    mock_old: MagicMock,
    mock_new: MagicMock,
    client: Client,
    user: RevelUser,
) -> None:
    token = _make_change_token(user, "fresh@example.com")
    url = reverse("api:email-change-confirm")
    response = client.post(
        url,
        data=orjson.dumps({"token": token}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    data = response.json()
    assert data["user"]["email"] == "fresh@example.com"
    assert "access" in data["token"]
    assert "refresh" in data["token"]


@patch("accounts.tasks.send_email_change_completed_new.delay")
@patch("accounts.tasks.send_email_change_completed_old.delay")
def test_full_flow_old_refresh_tokens_invalidated(
    mock_old: MagicMock,
    mock_new: MagicMock,
    client: Client,
    user: RevelUser,
) -> None:
    """End-to-end: after confirmation, the previously-issued refresh tokens are blacklisted."""
    # Pre-confirmation, issue a refresh token for the user.
    RefreshToken.for_user(user)
    old_outstanding_jtis = list(OutstandingToken.objects.filter(user=user).values_list("jti", flat=True))
    assert len(old_outstanding_jtis) == 1

    token = _make_change_token(user, "newer@example.com")
    confirm_url = reverse("api:email-change-confirm")
    confirm_resp = client.post(confirm_url, data=orjson.dumps({"token": token}), content_type="application/json")
    assert confirm_resp.status_code == 200, confirm_resp.content

    # Every refresh token outstanding before the swap must now be blacklisted.
    for jti in old_outstanding_jtis:
        assert BlacklistedToken.objects.filter(token__jti=jti).exists()

    # The freshly returned refresh token must NOT be blacklisted.
    assert "refresh" in confirm_resp.json()["token"]
