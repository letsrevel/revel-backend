"""Repeat signup on an unverified address, superseded verification links and invitee enrolment."""

import datetime
import re
import typing as t
from unittest.mock import MagicMock, patch

import pytest
from django.conf import settings
from django.test.client import Client
from django.urls import reverse
from freezegun import freeze_time
from ninja.errors import HttpError
from ninja_jwt.tokens import RefreshToken

from accounts import schema
from accounts.models import (
    EmailVerificationReminderTracking,
    Referral,
    ReferralApplication,
    ReferralCode,
    RevelUser,
)
from accounts.service import account as account_service
from accounts.service import referral_application_service
from accounts.tasks import AccountEmail, send_account_email
from common.models import SiteSettings
from conftest import RevelUserFactory
from telegram.models import TelegramUser

pytestmark = pytest.mark.django_db

EMAIL = "victim@example.com"
OLD_PASSWORD = "old-Strong-password-123!"
NEW_PASSWORD = "new-Strong-password-456!"


def _payload(**overrides: t.Any) -> schema.RegisterUserSchema:
    data: dict[str, t.Any] = {
        "email": EMAIL,
        "password1": NEW_PASSWORD,
        "password2": NEW_PASSWORD,
        "first_name": "Second",
        "last_name": "Signup",
        "accept_toc_and_privacy": True,
        **overrides,
    }
    return schema.RegisterUserSchema(**data)


@pytest.fixture
def mock_delay() -> t.Iterator[MagicMock]:
    with patch("accounts.tasks.send_account_email.delay") as m:
        yield m


@pytest.fixture
def unverified(django_user_model: type[RevelUser]) -> RevelUser:
    """An unverified account whose credentials were set by the first signup."""
    return django_user_model.objects.create_user(
        username=EMAIL,
        email=EMAIL,
        password=OLD_PASSWORD,
        first_name="First",
        last_name="Signup",
        email_verified=False,
        totp_active=True,
    )


def _verification_token(mock_delay: MagicMock) -> str:
    calls = [c for c in mock_delay.call_args_list if c.args[0] == AccountEmail.VERIFICATION]
    return str(calls[-1].kwargs["token"])


def _me(access: str) -> int:
    return Client().get(reverse("api:me"), HTTP_AUTHORIZATION=f"Bearer {access}").status_code


def _login_status(password: str) -> int:
    response = Client().post(
        reverse("api:token_obtain_pair"),
        data={"username": EMAIL, "password": password},
        content_type="application/json",
    )
    return response.status_code


class TestRepeatSignupOnUnverifiedAccount:
    def test_start_over_resets_credentials_and_sessions(
        self,
        unverified: RevelUser,
        mock_delay: MagicMock,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        TelegramUser.objects.create(telegram_id=1234, user=unverified)
        old_secret = unverified.totp_secret
        with freeze_time("2026-01-01 12:00:00"):
            old_refresh = RefreshToken.for_user(unverified)
            old_access = str(old_refresh.access_token)  # type: ignore[attr-defined]
            with django_capture_on_commit_callbacks(execute=True):
                account_service.register_user(_payload(first_name="Newcomer"))
            old_link = _verification_token(mock_delay)
        mock_delay.reset_mock()

        with freeze_time("2026-01-01 12:00:30"):
            with django_capture_on_commit_callbacks(execute=True):
                user, _ = account_service.register_user(_payload())
            new_link = _verification_token(mock_delay)

            unverified.refresh_from_db()
            assert user.pk == unverified.pk
            assert (unverified.first_name, unverified.last_name) == ("Second", "Signup")
            assert unverified.totp_active is False
            assert unverified.totp_secret != old_secret
            assert unverified.email_verified is False
            assert not TelegramUser.objects.get(telegram_id=1234).user
            assert not unverified.check_password(OLD_PASSWORD)
            assert unverified.check_password(NEW_PASSWORD)

            # Sessions from before the second signup are refused.
            assert _me(old_access) == 401
            refresh_response = Client().post(
                reverse("api:token_refresh"), data={"refresh": str(old_refresh)}, content_type="application/json"
            )
            assert refresh_response.status_code != 200
            assert _login_status(OLD_PASSWORD) != 200

            # The link from the first signup is superseded, the newest one verifies.
            with pytest.raises(HttpError) as exc:
                account_service.verify_email(old_link)
            assert exc.value.status_code == 400
            assert account_service.verify_email(new_link).email_verified is True
            assert _login_status(NEW_PASSWORD) == 200

    def test_replaces_old_referral_and_applies_new_code(
        self, unverified: RevelUser, mock_delay: MagicMock, revel_user_factory: RevelUserFactory
    ) -> None:
        old_owner = revel_user_factory(email="old-owner@example.com")
        new_owner = revel_user_factory(email="new-owner@example.com")
        old_code = ReferralCode.objects.create(user=old_owner, code="OLDCODE")
        new_code = ReferralCode.objects.create(user=new_owner, code="NEWCODE")
        Referral.objects.create(referral_code=old_code, referred_user=unverified)

        account_service.register_user(_payload(referral_code="newcode"))

        assert Referral.objects.get(referred_user=unverified).referral_code == new_code

    def test_without_code_drops_old_referral(
        self, unverified: RevelUser, mock_delay: MagicMock, revel_user_factory: RevelUserFactory
    ) -> None:
        code = ReferralCode.objects.create(user=revel_user_factory(email="o@example.com"), code="OLDCODE")
        Referral.objects.create(referral_code=code, referred_user=unverified)

        account_service.register_user(_payload())

        assert not Referral.objects.filter(referred_user=unverified).exists()

    def test_invalid_code_changes_nothing(
        self, unverified: RevelUser, mock_delay: MagicMock, revel_user_factory: RevelUserFactory
    ) -> None:
        code = ReferralCode.objects.create(user=revel_user_factory(email="o@example.com"), code="OLDCODE")
        Referral.objects.create(referral_code=code, referred_user=unverified)
        TelegramUser.objects.create(telegram_id=99, user=unverified)
        password_hash = unverified.password

        with pytest.raises(HttpError) as exc:
            account_service.register_user(_payload(referral_code="nope"))

        assert exc.value.status_code == 422
        unverified.refresh_from_db()
        assert unverified.password == password_hash
        assert unverified.first_name == "First"
        assert unverified.totp_active is True
        assert unverified.credentials_changed_at is None
        assert TelegramUser.objects.get(telegram_id=99).user == unverified
        assert Referral.objects.get(referred_user=unverified).referral_code == code
        mock_delay.assert_not_called()

    def test_verified_duplicate_is_still_refused(self, unverified: RevelUser, mock_delay: MagicMock) -> None:
        unverified.email_verified = True
        unverified.save(update_fields=["email_verified"])
        with pytest.raises(HttpError) as exc:
            account_service.register_user(_payload())
        assert exc.value.status_code == 400
        unverified.refresh_from_db()
        assert unverified.check_password(OLD_PASSWORD)
        assert unverified.totp_active is True
        mock_delay.assert_not_called()

    def test_guest_duplicate_still_sends_activation_and_400(self, guest_user: RevelUser, mock_delay: MagicMock) -> None:
        password_hash = guest_user.password
        with pytest.raises(HttpError) as exc:
            account_service.register_user(_payload(email=guest_user.email))
        assert exc.value.status_code == 400
        guest_user.refresh_from_db()
        assert guest_user.guest is True
        assert guest_user.password == password_hash
        assert mock_delay.call_args.args[0] == AccountEmail.ACTIVATION

    def test_endpoint_returns_201(self, unverified: RevelUser, mock_delay: MagicMock) -> None:
        response = Client().post(
            reverse("api:register-account"),
            data=_payload().model_dump_json(),
            content_type="application/json",
        )
        assert response.status_code == 201
        unverified.refresh_from_db()
        assert unverified.check_password(NEW_PASSWORD)


class TestVerificationTokenIssueTime:
    def test_same_second_token_after_reset_is_accepted(self, unverified: RevelUser) -> None:
        with freeze_time("2026-01-01 12:00:00.700000"):
            account_service.reset_unverified_credentials(unverified, password=NEW_PASSWORD)
            token = account_service.create_verification_token(unverified)
            assert account_service.verify_email(token).email_verified is True

    def test_token_minted_before_reset_is_refused(self, unverified: RevelUser) -> None:
        with freeze_time("2026-01-01 12:00:00"):
            token = account_service.create_verification_token(unverified)
        with freeze_time("2026-01-01 12:00:05"):
            account_service.reset_unverified_credentials(unverified, password=NEW_PASSWORD)
            with pytest.raises(HttpError) as exc:
                account_service.verify_email(token)
        assert exc.value.status_code == 400
        unverified.refresh_from_db()
        assert unverified.email_verified is False

    def test_token_without_iat_is_refused_after_reset_but_not_before(self, unverified: RevelUser) -> None:
        from accounts.jwt import create_token
        from common.keys import signing_key

        def legacy_token() -> str:
            payload = schema.VerifyEmailJWTPayloadSchema(
                user_id=unverified.id,
                email=unverified.email,
                exp=datetime.datetime.now(datetime.UTC) + settings.VERIFY_TOKEN_LIFETIME,
            ).model_dump(mode="json", exclude_none=True)
            return create_token(payload, signing_key(), settings.JWT_ALGORITHM)

        # Never-reset account: a token minted before this change keeps working.
        assert account_service.verify_email(legacy_token()).email_verified is True
        unverified.email_verified = False
        unverified.save(update_fields=["email_verified"])
        account_service.reset_unverified_credentials(unverified, password=NEW_PASSWORD)
        with pytest.raises(HttpError):
            account_service.verify_email(legacy_token())


class TestPasswordResetOnUnverifiedAccount:
    def _reset(self, user: RevelUser) -> None:
        token = account_service.create_password_reset_token(user)
        account_service.reset_password(token, NEW_PASSWORD)

    def test_verifies_and_drops_second_factor_and_telegram(self, unverified: RevelUser) -> None:
        TelegramUser.objects.create(telegram_id=5, user=unverified)
        EmailVerificationReminderTracking.objects.create(user=unverified)
        old_secret = unverified.totp_secret

        self._reset(unverified)

        unverified.refresh_from_db()
        assert unverified.email_verified is True
        assert unverified.is_active is True
        assert unverified.totp_active is False
        assert unverified.totp_secret != old_secret
        assert not TelegramUser.objects.get(telegram_id=5).user
        assert not EmailVerificationReminderTracking.objects.filter(user=unverified).exists()
        assert unverified.check_password(NEW_PASSWORD)

    def test_verified_account_keeps_totp(self, unverified: RevelUser) -> None:
        unverified.email_verified = True
        unverified.save(update_fields=["email_verified"])
        TelegramUser.objects.create(telegram_id=6, user=unverified)
        old_secret = unverified.totp_secret

        self._reset(unverified)

        unverified.refresh_from_db()
        assert unverified.totp_active is True
        assert unverified.totp_secret == old_secret
        assert TelegramUser.objects.get(telegram_id=6).user == unverified


@pytest.fixture
def approved_application() -> ReferralApplication:
    return ReferralApplication.objects.create(
        email="invitee@example.com",
        code="invited",
        note="hi",
        status=ReferralApplication.Status.APPROVED,
    )


class TestInviteeEnrolmentRequiresVerifiedEmail:
    def test_unverified_signup_is_not_enrolled_until_verified(
        self,
        approved_application: ReferralApplication,
        mock_delay: MagicMock,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        with django_capture_on_commit_callbacks(execute=True):
            user, _ = account_service.register_user(_payload(email="invitee@example.com"))
        approved_application.refresh_from_db()
        assert approved_application.user_id is None
        assert not ReferralCode.objects.filter(user=user).exists()

        account_service.verify_email(_verification_token(mock_delay))

        approved_application.refresh_from_db()
        assert approved_application.user_id == user.pk
        assert ReferralCode.objects.filter(user=user, code="invited").exists()

    def test_approve_with_existing_unverified_user_sends_invite(
        self, django_user_model: type[RevelUser], mock_delay: MagicMock, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        user = django_user_model.objects.create_user(
            username="invitee@example.com", email="invitee@example.com", password="x", email_verified=False
        )
        application = ReferralApplication.objects.create(email="invitee@example.com", code="invited", note="hi")
        staff = django_user_model.objects.create_user(username="s@example.com", email="s@example.com", password="x")

        with django_capture_on_commit_callbacks(execute=True):
            referral_application_service.approve(application, actor=staff)

        application.refresh_from_db()
        assert application.user is None
        assert not ReferralCode.objects.filter(user=user).exists()
        assert mock_delay.call_args.args[0] == AccountEmail.REFERRAL_INVITE

    @pytest.mark.parametrize("variant", ["invitee+tag@example.com", "inv.itee@example.com"])
    def test_variant_address_is_not_enrolled(
        self, approved_application: ReferralApplication, django_user_model: type[RevelUser], variant: str
    ) -> None:
        user = django_user_model.objects.create_user(username=variant, email=variant, password="x", email_verified=True)
        referral_application_service.try_enroll_invitee(user)
        approved_application.refresh_from_db()
        assert approved_application.user is None
        assert not ReferralCode.objects.filter(user=user).exists()

    def test_exact_address_other_case_is_enrolled(
        self, approved_application: ReferralApplication, django_user_model: type[RevelUser]
    ) -> None:
        user = django_user_model.objects.create_user(
            username="Invitee@Example.com", email="Invitee@Example.com", password="x", email_verified=True
        )
        referral_application_service.try_enroll_invitee(user)
        approved_application.refresh_from_db()
        assert approved_application.user == user


class TestVerificationEmailDeletionLink:
    def test_bodies_carry_deletion_link(self, unverified: RevelUser, mock_delay: MagicMock) -> None:
        with patch("accounts.tasks.email.send_email") as mock_send:
            account_service.send_verification_email_for_user(unverified, defer=False, return_host="events.example.org")
            kwargs = mock_delay.call_args.kwargs
            send_account_email(AccountEmail.VERIFICATION, unverified.email, **kwargs)

        base = re.escape(SiteSettings.get_solo().frontend_base_url)
        pattern = rf"{base}/account/confirm-deletion\?token=[\w.-]+&return_host=events.example.org"
        assert re.search(pattern, mock_send.call_args.kwargs["body"])
        assert re.search(pattern, mock_send.call_args.kwargs["html_body"].replace("&amp;", "&"))

    def test_old_message_without_deletion_token_still_sends(self, unverified: RevelUser) -> None:
        with patch("accounts.tasks.email.send_email") as mock_send:
            send_account_email(AccountEmail.VERIFICATION, unverified.email, token="tok", context={})
        assert "confirm-deletion" not in mock_send.call_args.kwargs["body"]
        assert "/login/confirm-email?token=tok" in mock_send.call_args.kwargs["body"]
