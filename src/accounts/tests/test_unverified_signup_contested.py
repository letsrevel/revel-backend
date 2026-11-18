"""A repeat signup on an unverified address only marks it contested; verifying it later restarts the account."""

import typing as t
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.base import ContentFile
from django.test.client import Client
from django.utils import timezone
from django.urls import reverse
from ninja.errors import HttpError
from ninja_jwt.token_blacklist.models import BlacklistedToken
from ninja_jwt.tokens import RefreshToken
from oauth2_provider.models import AccessToken

from accounts import schema
from accounts.models import EmailVerificationReminderTracking, Referral, ReferralCode, RevelUser
from accounts.service import account as account_service
from accounts.tasks import AccountEmail
from conftest import RevelUserFactory
from oauth.models import OAuthApplication
from telegram.models import TelegramUser

pytestmark = pytest.mark.django_db

EMAIL = "stored@example.com"
STORED_PASSWORD = "stored-Strong-password-123!"
SUBMITTED_PASSWORD = "submitted-Strong-password-456!"


def _payload(**overrides: t.Any) -> schema.RegisterUserSchema:
    data: dict[str, t.Any] = {
        "email": EMAIL,
        "password1": SUBMITTED_PASSWORD,
        "password2": SUBMITTED_PASSWORD,
        "first_name": "Submitted",
        "last_name": "Person",
        "accept_toc_and_privacy": True,
        **overrides,
    }
    return schema.RegisterUserSchema(**data)


@pytest.fixture
def mock_delay() -> t.Iterator[MagicMock]:
    with patch("accounts.tasks.send_account_email.delay") as m:
        yield m


@pytest.fixture
def referral(revel_user_factory: RevelUserFactory) -> ReferralCode:
    return ReferralCode.objects.create(user=revel_user_factory(email="referrer@example.com"), code="STORED")


@pytest.fixture
def stored(django_user_model: type[RevelUser], referral: ReferralCode) -> RevelUser:
    """An unverified account with a full profile, 2FA, a Telegram link, a referral and a live session."""
    user = django_user_model.objects.create_user(
        username=EMAIL,
        email=EMAIL,
        password=STORED_PASSWORD,
        first_name="Stored",
        last_name="Owner",
        preferred_name="Stored Pref",
        pronouns="they/them",
        bio="stored bio",
        phone_number="+4366012345678",
        language="de",
        totp_active=True,
        email_verified=False,
    )
    user.profile_picture.save("a.png", ContentFile(b"x"), save=True)
    Referral.objects.create(referral_code=referral, referred_user=user)
    TelegramUser.objects.create(telegram_id=4242, user=user)
    EmailVerificationReminderTracking.objects.create(user=user)
    return user


def _app_token_for(user: RevelUser) -> AccessToken:
    """A live OAuth app access token held by ``user``."""
    app = OAuthApplication(
        user=user,
        name="Test App",
        client_type=OAuthApplication.CLIENT_CONFIDENTIAL,
        redirect_uris="https://app.example/cb",
        allowed_scopes=["org:read"],
        registration_source=OAuthApplication.RegistrationSource.MANUAL,
    )
    app.save()  # no full_clean(): the OIDC signing key is irrelevant here
    return AccessToken.objects.create(
        user=user, application=app, token="app-token-checksum", scope="org:read", expires=timezone.now() + timedelta(1)
    )


def _sent_links(mock_delay: MagicMock) -> list[str]:
    return [str(c.kwargs["token"]) for c in mock_delay.call_args_list if c.args[0] == AccountEmail.VERIFICATION]


def _register(**overrides: t.Any) -> t.Any:
    return Client().post(
        reverse("api:register-account"), data=_payload(**overrides).model_dump_json(), content_type="application/json"
    )


class TestRepeatSignup:
    def test_changes_nothing_stored_and_echoes_only_the_payload(
        self, stored: RevelUser, mock_delay: MagicMock, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        refresh = RefreshToken.for_user(stored)
        old_hash, old_secret = stored.password, stored.totp_secret
        old_picture = stored.profile_picture.name

        with django_capture_on_commit_callbacks(execute=True):
            response = _register()

        assert response.status_code == 201
        body = response.json()
        assert (body["first_name"], body["last_name"], body["email"]) == ("Submitted", "Person", EMAIL)
        assert body["id"] != str(stored.pk)
        assert body["totp_active"] is False
        assert (body["preferred_name"], body["bio"], body["pronouns"]) == ("", "", "")
        assert body["language"] != "de"

        stored.refresh_from_db()
        assert stored.signup_contested_at is not None
        assert stored.password == old_hash
        assert stored.check_password(STORED_PASSWORD)
        assert (stored.first_name, stored.last_name) == ("Stored", "Owner")
        assert (stored.preferred_name, stored.pronouns, stored.bio) == ("Stored Pref", "they/them", "stored bio")
        assert stored.phone_number == "+4366012345678"
        assert stored.profile_picture.name == old_picture
        assert stored.language == "de"
        assert stored.totp_active is True and stored.totp_secret == old_secret
        assert stored.credentials_changed_at is None
        assert stored.email_verified is False
        assert Referral.objects.filter(referred_user=stored, referral_code__code="STORED").exists()
        assert TelegramUser.objects.get(telegram_id=4242).user == stored
        assert not BlacklistedToken.objects.filter(token__jti=refresh["jti"]).exists()
        assert EmailVerificationReminderTracking.objects.filter(user=stored).exists()
        assert len(_sent_links(mock_delay)) == 1

    def test_referral_code_is_not_applied(
        self, stored: RevelUser, mock_delay: MagicMock, revel_user_factory: RevelUserFactory
    ) -> None:
        ReferralCode.objects.create(user=revel_user_factory(email="other@example.com"), code="OTHER")
        assert _register(referral_code="other").status_code == 201
        assert Referral.objects.get(referred_user=stored).referral_code.code == "STORED"

    def test_invalid_referral_code_changes_nothing(self, stored: RevelUser, mock_delay: MagicMock) -> None:
        with pytest.raises(HttpError) as exc:
            account_service.register_user(_payload(referral_code="nope"))
        assert exc.value.status_code == 422
        stored.refresh_from_db()
        assert stored.signup_contested_at is None
        mock_delay.assert_not_called()

    def test_verified_duplicate_is_still_refused(self, stored: RevelUser, mock_delay: MagicMock) -> None:
        RevelUser.objects.filter(pk=stored.pk).update(email_verified=True)
        assert _register().status_code == 400
        stored.refresh_from_db()
        assert stored.signup_contested_at is None
        mock_delay.assert_not_called()

    def test_guest_duplicate_sends_activation_and_is_refused(
        self, guest_user: RevelUser, mock_delay: MagicMock
    ) -> None:
        assert _register(email=guest_user.email).status_code == 400
        guest_user.refresh_from_db()
        assert guest_user.signup_contested_at is None
        assert mock_delay.call_args.args[0] == AccountEmail.ACTIVATION


class TestVerifyContestedAccount:
    @pytest.mark.parametrize("link", ["old", "new"])
    def test_verifying_restarts_the_account(
        self,
        stored: RevelUser,
        mock_delay: MagicMock,
        django_capture_on_commit_callbacks: t.Any,
        link: str,
    ) -> None:
        old_link = account_service.create_verification_token(stored)
        old_refresh = RefreshToken.for_user(stored)
        old_picture = stored.profile_picture.name
        old_secret = stored.totp_secret
        app_token = _app_token_for(stored)
        with django_capture_on_commit_callbacks(execute=True):
            _register()
        new_link = _sent_links(mock_delay)[-1]
        mock_delay.reset_mock()

        with django_capture_on_commit_callbacks(execute=True):
            response = Client().post(
                reverse("api:verify-email"),
                data={"token": old_link if link == "old" else new_link},
                content_type="application/json",
            )

        assert response.status_code == 200
        assert response.json()["token"]["access"]
        stored.refresh_from_db()
        assert stored.email_verified is True and stored.signup_contested_at is None
        assert stored.has_usable_password() is False
        assert stored.totp_active is False
        assert stored.totp_secret != old_secret
        assert stored.credentials_changed_at is not None
        assert not AccessToken.objects.filter(pk=app_token.pk).exists()
        assert not TelegramUser.objects.get(telegram_id=4242).user
        assert BlacklistedToken.objects.filter(token__jti=old_refresh["jti"]).exists()
        assert (stored.preferred_name, stored.pronouns, stored.bio) == ("", "", "")
        assert stored.phone_number is None
        assert not stored.profile_picture and old_picture
        assert (stored.first_name, stored.last_name, stored.language) == ("Stored", "Owner", "de")
        assert not Referral.objects.filter(referred_user=stored).exists()
        assert not EmailVerificationReminderTracking.objects.filter(user=stored).exists()
        assert [c.args[0] for c in mock_delay.call_args_list] == [AccountEmail.PASSWORD_RESET]
        assert mock_delay.call_args.args[1] == EMAIL

    def test_uncontested_account_keeps_everything(
        self, stored: RevelUser, mock_delay: MagicMock, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        link = account_service.create_verification_token(stored)

        with django_capture_on_commit_callbacks(execute=True):
            assert account_service.verify_email(link).email_verified is True

        stored.refresh_from_db()
        assert stored.check_password(STORED_PASSWORD)
        assert stored.totp_active is True
        assert (stored.preferred_name, stored.bio) == ("Stored Pref", "stored bio")
        assert Referral.objects.filter(referred_user=stored).exists()
        assert TelegramUser.objects.get(telegram_id=4242).user == stored
        mock_delay.assert_not_called()

    def test_verify_locks_the_user_row(self, stored: RevelUser) -> None:
        link = account_service.create_verification_token(stored)
        with patch.object(
            RevelUser.objects.__class__,
            "select_for_update",
            autospec=True,
            side_effect=RevelUser.objects.select_for_update,
        ) as spy:
            account_service.verify_email(link)
        assert spy.called


class TestPasswordResetOnContestedAccount:
    def test_reset_adopts_with_the_new_password_and_resets_the_profile(
        self, stored: RevelUser, mock_delay: MagicMock
    ) -> None:
        _register()
        token = account_service.create_password_reset_token(stored)

        account_service.reset_password(token, SUBMITTED_PASSWORD)

        stored.refresh_from_db()
        assert stored.check_password(SUBMITTED_PASSWORD)
        assert stored.email_verified is True and stored.signup_contested_at is None
        assert stored.totp_active is False
        assert not TelegramUser.objects.get(telegram_id=4242).user
        assert (stored.preferred_name, stored.bio) == ("", "")
        assert not Referral.objects.filter(referred_user=stored).exists()

    def test_reset_without_contest_keeps_the_profile(self, stored: RevelUser) -> None:
        token = account_service.create_password_reset_token(stored)

        account_service.reset_password(token, SUBMITTED_PASSWORD)

        stored.refresh_from_db()
        assert stored.check_password(SUBMITTED_PASSWORD)
        assert stored.email_verified is True
        assert stored.totp_active is False
        assert (stored.preferred_name, stored.bio) == ("Stored Pref", "stored bio")
        assert Referral.objects.filter(referred_user=stored).exists()

    def test_reset_reads_the_row_it_locked(self, stored: RevelUser, mock_delay: MagicMock) -> None:
        """A contest committed after the caller loaded the user is still seen (adopts instead of keeping the profile)."""
        token = account_service.create_password_reset_token(stored)
        stale = RevelUser.objects.get(pk=stored.pk)
        _register()  # commits the contest after ``stale`` was loaded
        assert stale.signup_contested_at is None

        account_service._apply_password_reset(stale, token, SUBMITTED_PASSWORD)

        stored.refresh_from_db()
        assert (stored.preferred_name, stored.bio) == ("", "")
        assert stored.signup_contested_at is None
