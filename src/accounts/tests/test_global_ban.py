"""Tests for global ban system: model, service, signal, task, admin action, and integration guards."""

import typing as t
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.db.models.signals import post_save
from django.test.client import Client
from django.utils import timezone
from ninja.errors import HttpError

from accounts import schema
from accounts.models import GlobalBan, RevelUser
from accounts.service import account as account_service
from accounts.service import auth as auth_service
from accounts.service.global_ban_service import (
    _blacklist_user_tokens,
    deactivate_user_for_ban,
    is_email_globally_banned,
    is_telegram_globally_banned,
    process_domain_ban,
    process_email_ban,
    process_telegram_ban,
)
from accounts.signals import handle_global_ban_created

pytestmark = pytest.mark.django_db


# ===== Helpers =====


def _save_ban_without_signal(ban: GlobalBan) -> None:
    """Save a GlobalBan without triggering the post_save signal handler."""
    post_save.disconnect(handle_global_ban_created, sender=GlobalBan)
    try:
        ban.save()
    finally:
        post_save.connect(handle_global_ban_created, sender=GlobalBan)


# ===== Fixtures =====


@pytest.fixture
def admin_user(django_user_model: t.Type[RevelUser]) -> RevelUser:
    """A superuser who creates bans."""
    return django_user_model.objects.create_superuser(
        username="admin@revel.test",
        email="admin@revel.test",
        password="strong-password-123!",
    )


@pytest.fixture
def target_user(django_user_model: t.Type[RevelUser]) -> RevelUser:
    """A regular user who will be banned."""
    return django_user_model.objects.create_user(
        username="target@example.com",
        email="target@example.com",
        password="strong-password-123!",
        is_active=True,
        email_verified=True,
    )


@pytest.fixture
def email_ban(admin_user: RevelUser) -> GlobalBan:
    """A GlobalBan of type EMAIL (signal handler disabled to avoid side-effects)."""
    ban = GlobalBan(
        ban_type=GlobalBan.BanType.EMAIL,
        value="banned@example.com",
        reason="Test ban",
        created_by=admin_user,
    )
    _save_ban_without_signal(ban)
    return ban


# ===== Model Tests =====


class TestGlobalBanModel:
    """Tests for GlobalBan model save() normalization and constraints."""

    def test_email_normalization_on_save(self, admin_user: RevelUser) -> None:
        ban = GlobalBan.objects.create(
            ban_type=GlobalBan.BanType.EMAIL,
            value="User+Tag@Gmail.com",
            created_by=admin_user,
        )
        assert ban.normalized_value == "user@gmail.com"

    def test_domain_normalization_on_save(self, admin_user: RevelUser) -> None:
        ban = GlobalBan.objects.create(
            ban_type=GlobalBan.BanType.DOMAIN,
            value="  EVIL.COM  ",
            created_by=admin_user,
        )
        assert ban.normalized_value == "evil.com"

    def test_telegram_normalization_on_save(self, admin_user: RevelUser) -> None:
        ban = GlobalBan.objects.create(
            ban_type=GlobalBan.BanType.TELEGRAM,
            value="@BadUser",
            created_by=admin_user,
        )
        assert ban.normalized_value == "baduser"

    def test_unique_constraint(self, admin_user: RevelUser) -> None:
        # TimeStampedModel.save() calls full_clean() before hitting the database,
        # so a duplicate raises ValidationError (from Django validation) rather than
        # IntegrityError (from the DB constraint).
        GlobalBan.objects.create(
            ban_type=GlobalBan.BanType.EMAIL,
            value="dupe@example.com",
            created_by=admin_user,
        )
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError, match="Ban type.*Normalized value.*already exists"):
            GlobalBan.objects.create(
                ban_type=GlobalBan.BanType.EMAIL,
                value="Dupe@Example.com",  # Same after normalization
                created_by=admin_user,
            )

    def test_str_representation(self, admin_user: RevelUser) -> None:
        ban = GlobalBan(ban_type=GlobalBan.BanType.EMAIL, value="test@example.com")
        assert "test@example.com" in str(ban)


# ===== Service Tests =====


class TestIsEmailGloballyBanned:
    """Tests for is_email_globally_banned."""

    def test_banned_email_exact(self, admin_user: RevelUser) -> None:
        GlobalBan.objects.create(
            ban_type=GlobalBan.BanType.EMAIL,
            value="bad@example.com",
            created_by=admin_user,
        )
        assert is_email_globally_banned("bad@example.com") is True

    def test_banned_email_case_insensitive(self, admin_user: RevelUser) -> None:
        GlobalBan.objects.create(
            ban_type=GlobalBan.BanType.EMAIL,
            value="bad@example.com",
            created_by=admin_user,
        )
        assert is_email_globally_banned("BAD@EXAMPLE.COM") is True

    def test_banned_email_tagged_variant(self, admin_user: RevelUser) -> None:
        GlobalBan.objects.create(
            ban_type=GlobalBan.BanType.EMAIL,
            value="bad@gmail.com",
            created_by=admin_user,
        )
        assert is_email_globally_banned("bad+sneaky@gmail.com") is True

    def test_banned_domain(self, admin_user: RevelUser) -> None:
        GlobalBan.objects.create(
            ban_type=GlobalBan.BanType.DOMAIN,
            value="evil.com",
            created_by=admin_user,
        )
        assert is_email_globally_banned("anyone@evil.com") is True

    def test_unbanned_email(self) -> None:
        assert is_email_globally_banned("clean@example.com") is False

    def test_telegram_ban_does_not_match_email(self, admin_user: RevelUser) -> None:
        GlobalBan.objects.create(
            ban_type=GlobalBan.BanType.TELEGRAM,
            value="baduser",
            created_by=admin_user,
        )
        assert is_email_globally_banned("baduser@example.com") is False


class TestIsTelegramGloballyBanned:
    """Tests for is_telegram_globally_banned."""

    def test_banned_username(self, admin_user: RevelUser) -> None:
        GlobalBan.objects.create(
            ban_type=GlobalBan.BanType.TELEGRAM,
            value="@baduser",
            created_by=admin_user,
        )
        assert is_telegram_globally_banned("baduser") is True

    def test_banned_username_case_insensitive(self, admin_user: RevelUser) -> None:
        GlobalBan.objects.create(
            ban_type=GlobalBan.BanType.TELEGRAM,
            value="baduser",
            created_by=admin_user,
        )
        assert is_telegram_globally_banned("@BadUser") is True

    def test_unbanned_username(self) -> None:
        assert is_telegram_globally_banned("cleanuser") is False


class TestDeactivateUserForBan:
    """Tests for deactivate_user_for_ban."""

    @patch("notifications.signals.notification_requested")
    def test_deactivates_active_user(self, mock_signal: MagicMock, target_user: RevelUser) -> None:
        deactivate_user_for_ban(target_user, reason="Bad behavior")
        target_user.refresh_from_db()
        assert target_user.is_active is False
        mock_signal.send.assert_called_once()

    @patch("notifications.signals.notification_requested")
    def test_skips_staff_user(self, mock_signal: MagicMock, django_user_model: t.Type[RevelUser]) -> None:
        staff = django_user_model.objects.create_user(
            username="staff@example.com",
            email="staff@example.com",
            password="strong-password-123!",
            is_staff=True,
            is_active=True,
        )
        deactivate_user_for_ban(staff, reason="Should be skipped")
        staff.refresh_from_db()
        assert staff.is_active is True
        mock_signal.send.assert_not_called()

    @patch("notifications.signals.notification_requested")
    def test_skips_superuser(self, mock_signal: MagicMock, django_user_model: t.Type[RevelUser]) -> None:
        superuser = django_user_model.objects.create_superuser(
            username="super@example.com",
            email="super@example.com",
            password="strong-password-123!",
        )
        deactivate_user_for_ban(superuser, reason="Should be skipped")
        superuser.refresh_from_db()
        assert superuser.is_active is True
        mock_signal.send.assert_not_called()

    @patch("notifications.signals.notification_requested")
    def test_skips_already_inactive_user(self, mock_signal: MagicMock, target_user: RevelUser) -> None:
        target_user.is_active = False
        target_user.save(update_fields=["is_active"])

        deactivate_user_for_ban(target_user, reason="Already inactive")
        mock_signal.send.assert_not_called()

    @patch("notifications.signals.notification_requested")
    def test_sends_notification_with_correct_context(self, mock_signal: MagicMock, target_user: RevelUser) -> None:
        deactivate_user_for_ban(target_user, reason="Spam")
        call_kwargs = mock_signal.send.call_args[1]
        assert call_kwargs["context"]["ban_reason"] == "Spam"
        assert call_kwargs["user"] == target_user

    @patch("notifications.signals.notification_requested")
    def test_sends_notification_before_deactivation(self, mock_signal: MagicMock, target_user: RevelUser) -> None:
        """Notification is sent while user is still active, ensuring delivery channels work."""
        user_was_active_at_notification = None

        def capture_active_state(**kwargs: t.Any) -> None:
            nonlocal user_was_active_at_notification
            user_was_active_at_notification = kwargs["user"].is_active

        mock_signal.send.side_effect = capture_active_state
        deactivate_user_for_ban(target_user, reason="Test ordering")

        assert user_was_active_at_notification is True
        target_user.refresh_from_db()
        assert target_user.is_active is False

    @patch("notifications.signals.notification_requested")
    def test_blacklists_outstanding_jwt_tokens(self, mock_signal: MagicMock, target_user: RevelUser) -> None:
        """All outstanding JWT tokens are blacklisted when user is banned."""
        from ninja_jwt.token_blacklist.models import BlacklistedToken, OutstandingToken
        from ninja_jwt.tokens import RefreshToken

        # Generate a token for the user (side-effect creates OutstandingToken)
        RefreshToken.for_user(target_user)
        assert OutstandingToken.objects.filter(user=target_user).exists()

        deactivate_user_for_ban(target_user, reason="Token test")

        outstanding = OutstandingToken.objects.filter(user=target_user)
        for ot in outstanding:
            assert BlacklistedToken.objects.filter(token=ot).exists()


class TestBlacklistUserTokens:
    """Tests for _blacklist_user_tokens."""

    def test_blacklists_tokens(self, target_user: RevelUser) -> None:
        from ninja_jwt.token_blacklist.models import BlacklistedToken, OutstandingToken
        from ninja_jwt.tokens import RefreshToken

        RefreshToken.for_user(target_user)
        RefreshToken.for_user(target_user)
        assert OutstandingToken.objects.filter(user=target_user).count() == 2

        count = _blacklist_user_tokens(target_user)

        assert count == 2
        assert BlacklistedToken.objects.filter(token__user=target_user).count() == 2

    def test_skips_already_blacklisted(self, target_user: RevelUser) -> None:
        from ninja_jwt.token_blacklist.models import BlacklistedToken
        from ninja_jwt.tokens import RefreshToken

        RefreshToken.for_user(target_user)
        _blacklist_user_tokens(target_user)
        assert BlacklistedToken.objects.filter(token__user=target_user).count() == 1

        # Running again should not create duplicates
        count = _blacklist_user_tokens(target_user)
        assert count == 0
        assert BlacklistedToken.objects.filter(token__user=target_user).count() == 1

    def test_no_tokens(self, target_user: RevelUser) -> None:
        count = _blacklist_user_tokens(target_user)
        assert count == 0


class TestProcessEmailBan:
    """Tests for process_email_ban."""

    @patch("accounts.service.global_ban_service.deactivate_user_for_ban")
    def test_finds_and_deactivates_user(
        self, mock_deactivate: MagicMock, target_user: RevelUser, admin_user: RevelUser
    ) -> None:
        ban = GlobalBan(
            ban_type=GlobalBan.BanType.EMAIL,
            value=target_user.email,
            reason="Banned",
            created_by=admin_user,
        )
        _save_ban_without_signal(ban)

        process_email_ban(ban)

        ban.refresh_from_db()
        assert ban.user == target_user
        mock_deactivate.assert_called_once_with(target_user, reason="Banned")

    @patch("accounts.service.global_ban_service.deactivate_user_for_ban")
    def test_gmail_dot_variant_not_found_retroactively(
        self, mock_deactivate: MagicMock, django_user_model: t.Type[RevelUser], admin_user: RevelUser
    ) -> None:
        """Banning the normalized form won't retroactively match a user stored with dots.

        The ban still prevents future logins via is_email_globally_banned().
        """
        django_user_model.objects.create_user(
            username="first.last@gmail.com",
            email="first.last@gmail.com",
            password="strong-password-123!",
            is_active=True,
        )
        ban = GlobalBan(
            ban_type=GlobalBan.BanType.EMAIL,
            value="firstlast@gmail.com",
            reason="Dot variant test",
            created_by=admin_user,
        )
        _save_ban_without_signal(ban)

        process_email_ban(ban)

        # User is NOT found because email__iexact doesn't match dot-variants
        mock_deactivate.assert_not_called()

        # But future logins are still blocked via normalization check
        assert is_email_globally_banned("first.last@gmail.com") is True

    @patch("accounts.service.global_ban_service.deactivate_user_for_ban")
    def test_no_matching_user(self, mock_deactivate: MagicMock, admin_user: RevelUser) -> None:
        ban = GlobalBan(
            ban_type=GlobalBan.BanType.EMAIL,
            value="nonexistent@example.com",
            created_by=admin_user,
        )
        _save_ban_without_signal(ban)

        process_email_ban(ban)
        mock_deactivate.assert_not_called()


class TestProcessTelegramBan:
    """Tests for process_telegram_ban."""

    @patch("accounts.service.global_ban_service.deactivate_user_for_ban")
    def test_finds_and_deactivates_user(
        self, mock_deactivate: MagicMock, target_user: RevelUser, admin_user: RevelUser
    ) -> None:
        from telegram.models import TelegramUser

        TelegramUser.objects.create(
            telegram_id=12345,
            telegram_username="target_tg",
            user=target_user,
        )
        ban = GlobalBan(
            ban_type=GlobalBan.BanType.TELEGRAM,
            value="@target_tg",
            reason="TG ban",
            created_by=admin_user,
        )
        _save_ban_without_signal(ban)

        process_telegram_ban(ban)

        ban.refresh_from_db()
        assert ban.user == target_user
        mock_deactivate.assert_called_once_with(target_user, reason="TG ban")

    @patch("accounts.service.global_ban_service.deactivate_user_for_ban")
    def test_no_matching_telegram_user(self, mock_deactivate: MagicMock, admin_user: RevelUser) -> None:
        ban = GlobalBan(
            ban_type=GlobalBan.BanType.TELEGRAM,
            value="@nobody",
            created_by=admin_user,
        )
        _save_ban_without_signal(ban)

        process_telegram_ban(ban)
        mock_deactivate.assert_not_called()


class TestProcessDomainBan:
    """Tests for process_domain_ban."""

    @patch("accounts.service.global_ban_service.deactivate_user_for_ban")
    def test_deactivates_all_matching_users(
        self,
        mock_deactivate: MagicMock,
        django_user_model: t.Type[RevelUser],
        admin_user: RevelUser,
    ) -> None:
        u1 = django_user_model.objects.create_user(username="a@evil.com", email="a@evil.com", password="pass123!")
        u2 = django_user_model.objects.create_user(username="b@evil.com", email="b@evil.com", password="pass123!")
        # Different domain — should NOT be deactivated
        django_user_model.objects.create_user(username="c@good.com", email="c@good.com", password="pass123!")

        ban = GlobalBan(
            ban_type=GlobalBan.BanType.DOMAIN,
            value="evil.com",
            created_by=admin_user,
        )
        _save_ban_without_signal(ban)

        count = process_domain_ban(ban)

        assert count == 2
        assert mock_deactivate.call_count == 2
        deactivated_users = {call.args[0] for call in mock_deactivate.call_args_list}
        assert deactivated_users == {u1, u2}

    def test_domain_ban_skips_staff_and_superusers(
        self,
        django_user_model: t.Type[RevelUser],
        admin_user: RevelUser,
    ) -> None:
        """Staff and superusers on the banned domain are not deactivated."""
        regular = django_user_model.objects.create_user(
            username="regular@evil.com", email="regular@evil.com", password="pass123!"
        )
        django_user_model.objects.create_user(
            username="staff@evil.com", email="staff@evil.com", password="pass123!", is_staff=True
        )
        django_user_model.objects.create_superuser(
            username="super@evil.com", email="super@evil.com", password="pass123!"
        )

        ban = GlobalBan(ban_type=GlobalBan.BanType.DOMAIN, value="evil.com", created_by=admin_user)
        _save_ban_without_signal(ban)

        count = process_domain_ban(ban)

        # Only the regular user should be deactivated (staff/super excluded by queryset)
        assert count == 1
        regular.refresh_from_db()
        assert regular.is_active is False


# ===== Signal Tests =====


class TestGlobalBanSignal:
    """Tests for the post_save signal on GlobalBan."""

    @patch("accounts.service.global_ban_service.deactivate_user_for_ban")
    def test_email_ban_triggers_process_email_ban(
        self, mock_deactivate: MagicMock, target_user: RevelUser, admin_user: RevelUser
    ) -> None:
        """Creating an EMAIL ban should synchronously process it."""
        GlobalBan.objects.create(
            ban_type=GlobalBan.BanType.EMAIL,
            value=target_user.email,
            reason="Signal test",
            created_by=admin_user,
        )
        mock_deactivate.assert_called_once()

    @patch("accounts.service.global_ban_service.deactivate_user_for_ban")
    def test_telegram_ban_triggers_process_telegram_ban(
        self, mock_deactivate: MagicMock, target_user: RevelUser, admin_user: RevelUser
    ) -> None:
        """Creating a TELEGRAM ban should synchronously process it."""
        from telegram.models import TelegramUser

        TelegramUser.objects.create(
            telegram_id=99999,
            telegram_username="target_signal",
            user=target_user,
        )
        GlobalBan.objects.create(
            ban_type=GlobalBan.BanType.TELEGRAM,
            value="@target_signal",
            reason="Signal test",
            created_by=admin_user,
        )
        mock_deactivate.assert_called_once()

    @pytest.mark.django_db(transaction=True)
    @patch("accounts.service.global_ban_service.deactivate_user_for_ban")
    def test_domain_ban_dispatches_celery_task(self, mock_deactivate: MagicMock, admin_user: RevelUser) -> None:
        """Creating a DOMAIN ban should dispatch a Celery task (runs eagerly in tests)."""
        GlobalBan.objects.create(
            ban_type=GlobalBan.BanType.DOMAIN,
            value="test-domain-signal.com",
            reason="Domain signal test",
            created_by=admin_user,
        )
        # In eager mode, the task runs immediately via on_commit
        # No users match this domain, so deactivate is not called
        mock_deactivate.assert_not_called()

    @patch("accounts.service.global_ban_service.process_email_ban")
    def test_update_does_not_trigger_signal(self, mock_process: MagicMock, admin_user: RevelUser) -> None:
        """Updating an existing ban should NOT trigger processing."""
        ban = GlobalBan.objects.create(
            ban_type=GlobalBan.BanType.EMAIL,
            value="noop@example.com",
            created_by=admin_user,
        )
        mock_process.reset_mock()

        ban.reason = "Updated reason"
        ban.save(update_fields=["reason"])
        mock_process.assert_not_called()


# ===== Celery Task Tests =====


class TestProcessDomainBanTask:
    """Tests for the process_domain_ban_task Celery task."""

    @patch("accounts.service.global_ban_service.deactivate_user_for_ban")
    def test_task_calls_service(
        self,
        mock_deactivate: MagicMock,
        django_user_model: t.Type[RevelUser],
        admin_user: RevelUser,
    ) -> None:
        from accounts.tasks import process_domain_ban_task

        django_user_model.objects.create_user(
            username="victim@badco.com", email="victim@badco.com", password="pass123!"
        )
        ban = GlobalBan(
            ban_type=GlobalBan.BanType.DOMAIN,
            value="badco.com",
            created_by=admin_user,
        )
        _save_ban_without_signal(ban)

        result = process_domain_ban_task(str(ban.id))

        assert result["domain"] == "badco.com"
        assert result["deactivated_count"] == 1
        mock_deactivate.assert_called_once()

    @patch("notifications.signals.notification_requested")
    def test_task_end_to_end_deactivates_users(
        self,
        mock_signal: MagicMock,
        django_user_model: t.Type[RevelUser],
        admin_user: RevelUser,
    ) -> None:
        """End-to-end: domain ban task finds and actually deactivates matching users."""
        from accounts.tasks import process_domain_ban_task

        u1 = django_user_model.objects.create_user(
            username="alice@banned-domain.com", email="alice@banned-domain.com", password="pass123!"
        )
        u2 = django_user_model.objects.create_user(
            username="bob@banned-domain.com", email="bob@banned-domain.com", password="pass123!"
        )
        safe = django_user_model.objects.create_user(
            username="carol@safe-domain.com", email="carol@safe-domain.com", password="pass123!"
        )

        ban = GlobalBan(
            ban_type=GlobalBan.BanType.DOMAIN,
            value="banned-domain.com",
            reason="E2E test",
            created_by=admin_user,
        )
        _save_ban_without_signal(ban)

        result = process_domain_ban_task(str(ban.id))

        assert result["deactivated_count"] == 2

        u1.refresh_from_db()
        u2.refresh_from_db()
        safe.refresh_from_db()
        assert u1.is_active is False
        assert u2.is_active is False
        assert safe.is_active is True


# ===== Integration Guard Tests =====


class TestRegistrationBanGuard:
    """Tests for ban check in register_user."""

    @patch("accounts.tasks.send_verification_email.delay")
    def test_registration_blocked_for_banned_email(self, mock_email: MagicMock, admin_user: RevelUser) -> None:
        GlobalBan.objects.create(
            ban_type=GlobalBan.BanType.EMAIL,
            value="banned@example.com",
            created_by=admin_user,
        )
        payload = schema.RegisterUserSchema(
            email="banned@example.com",
            password1="a-Strong-password-123!",
            password2="a-Strong-password-123!",
            first_name="Bad",
            last_name="User",
            accept_toc_and_privacy=True,
        )
        with pytest.raises(HttpError) as exc_info:
            account_service.register_user(payload)
        assert exc_info.value.status_code == 403
        assert "not available" in str(exc_info.value)

    @patch("accounts.tasks.send_verification_email.delay")
    def test_registration_blocked_for_banned_domain(self, mock_email: MagicMock, admin_user: RevelUser) -> None:
        GlobalBan.objects.create(
            ban_type=GlobalBan.BanType.DOMAIN,
            value="evil.org",
            created_by=admin_user,
        )
        payload = schema.RegisterUserSchema(
            email="newuser@evil.org",
            password1="a-Strong-password-123!",
            password2="a-Strong-password-123!",
            first_name="Evil",
            last_name="User",
            accept_toc_and_privacy=True,
        )
        with pytest.raises(HttpError) as exc_info:
            account_service.register_user(payload)
        assert exc_info.value.status_code == 403

    @patch("accounts.tasks.send_verification_email.delay")
    def test_registration_allowed_for_clean_email(self, mock_email: MagicMock) -> None:
        payload = schema.RegisterUserSchema(
            email="clean@example.com",
            password1="a-Strong-password-123!",
            password2="a-Strong-password-123!",
            first_name="Clean",
            last_name="User",
            accept_toc_and_privacy=True,
        )
        user, _token = account_service.register_user(payload)
        assert user.email == "clean@example.com"


class TestVerifyEmailBanGuard:
    """Tests for ban check in verify_email."""

    @patch("accounts.tasks.send_verification_email.delay")
    def test_verify_email_blocked_for_banned_user(
        self,
        mock_email: MagicMock,
        django_user_model: t.Type[RevelUser],
        admin_user: RevelUser,
    ) -> None:
        user = django_user_model.objects.create_user(
            username="tobanned@example.com",
            email="tobanned@example.com",
            password="strong-password-123!",
            email_verified=False,
        )
        token = account_service.create_verification_token(user)

        # Ban the user's email
        GlobalBan.objects.create(
            ban_type=GlobalBan.BanType.EMAIL,
            value="tobanned@example.com",
            created_by=admin_user,
        )

        with pytest.raises(HttpError) as exc_info:
            account_service.verify_email(token)
        assert exc_info.value.status_code == 403

    @patch("accounts.tasks.send_verification_email.delay")
    def test_verify_email_allowed_for_clean_user(
        self,
        mock_email: MagicMock,
        django_user_model: t.Type[RevelUser],
    ) -> None:
        user = django_user_model.objects.create_user(
            username="clean@example.com",
            email="clean@example.com",
            password="strong-password-123!",
            email_verified=False,
        )
        token = account_service.create_verification_token(user)
        verified_user = account_service.verify_email(token)
        assert verified_user.email_verified is True


class TestGoogleLoginBanGuard:
    """Tests for ban check in google_login."""

    @patch("accounts.service.auth._verify_oauth2_token")
    def test_google_login_blocked_for_banned_email(
        self, mock_verify: MagicMock, admin_user: RevelUser, settings: t.Any
    ) -> None:
        settings.GOOGLE_SSO_STAFF_LIST = []
        settings.GOOGLE_SSO_SUPERUSER_LIST = []
        settings.GOOGLE_SSO_ALWAYS_UPDATE_USER_DATA = False

        GlobalBan.objects.create(
            ban_type=GlobalBan.BanType.EMAIL,
            value="banned@gmail.com",
            created_by=admin_user,
        )

        mock_verify.return_value = schema.GoogleIDInfo(
            sub="123",
            email="banned@gmail.com",
            given_name="Bad",
            family_name="User",
        )

        with pytest.raises(HttpError) as exc_info:
            auth_service.google_login("fake-id-token")
        assert exc_info.value.status_code == 403

    @patch("accounts.service.auth._verify_oauth2_token")
    def test_google_login_blocked_for_banned_domain(
        self, mock_verify: MagicMock, admin_user: RevelUser, settings: t.Any
    ) -> None:
        settings.GOOGLE_SSO_STAFF_LIST = []
        settings.GOOGLE_SSO_SUPERUSER_LIST = []
        settings.GOOGLE_SSO_ALWAYS_UPDATE_USER_DATA = False

        GlobalBan.objects.create(
            ban_type=GlobalBan.BanType.DOMAIN,
            value="evil.com",
            created_by=admin_user,
        )

        mock_verify.return_value = schema.GoogleIDInfo(
            sub="456",
            email="anyone@evil.com",
            given_name="Evil",
            family_name="Person",
        )

        with pytest.raises(HttpError) as exc_info:
            auth_service.google_login("fake-id-token")
        assert exc_info.value.status_code == 403


class TestTelegramConnectBanGuard:
    """Tests for ban check in telegram connect_accounts."""

    def test_connect_blocked_for_banned_telegram_user(
        self,
        target_user: RevelUser,
        admin_user: RevelUser,
    ) -> None:
        from telegram.models import AccountOTP, TelegramUser

        tg_user = TelegramUser.objects.create(
            telegram_id=55555,
            telegram_username="bannedtg",
        )
        otp = AccountOTP.objects.create(
            tg_user=tg_user,
            otp="123456789",
            expires_at=timezone.now() + timedelta(minutes=5),
        )

        GlobalBan.objects.create(
            ban_type=GlobalBan.BanType.TELEGRAM,
            value="@bannedtg",
            created_by=admin_user,
        )

        from telegram.service import connect_accounts

        with pytest.raises(HttpError) as exc_info:
            connect_accounts(target_user, otp.otp)
        assert exc_info.value.status_code == 403

        # User should be deactivated
        target_user.refresh_from_db()
        assert target_user.is_active is False

    def test_connect_invalidates_otp_on_ban(
        self,
        target_user: RevelUser,
        admin_user: RevelUser,
    ) -> None:
        """OTP is consumed when a banned Telegram user is detected."""
        from telegram.models import AccountOTP, TelegramUser

        tg_user = TelegramUser.objects.create(
            telegram_id=55556,
            telegram_username="bannedtg2",
        )
        otp = AccountOTP.objects.create(
            tg_user=tg_user,
            otp="987654321",
            expires_at=timezone.now() + timedelta(minutes=5),
        )

        GlobalBan.objects.create(
            ban_type=GlobalBan.BanType.TELEGRAM,
            value="@bannedtg2",
            created_by=admin_user,
        )

        from telegram.service import connect_accounts

        with pytest.raises(HttpError):
            connect_accounts(target_user, otp.otp)

        otp.refresh_from_db()
        assert otp.used_at is not None


# ===== Admin Action Tests =====


class TestBanSelectedUsersAction:
    """Tests for the 'ban selected users' admin action."""

    @pytest.fixture
    def admin_client(self, admin_user: RevelUser) -> Client:
        client = Client()
        client.force_login(admin_user)
        return client

    def test_ban_regular_user(self, admin_client: Client, target_user: RevelUser, admin_user: RevelUser) -> None:
        from django.urls import reverse

        url = reverse("admin:accounts_reveluser_changelist")
        response = admin_client.post(
            url,
            {
                "action": "ban_selected_users",
                "_selected_action": [str(target_user.pk)],
            },
        )
        assert response.status_code == 302  # Redirect after action

        assert GlobalBan.objects.filter(
            ban_type=GlobalBan.BanType.EMAIL,
            normalized_value=target_user.email,
        ).exists()

    def test_skip_staff_users(
        self,
        admin_client: Client,
        django_user_model: t.Type[RevelUser],
    ) -> None:
        from django.urls import reverse

        staff = django_user_model.objects.create_user(
            username="staff@example.com",
            email="staff@example.com",
            password="pass123!",
            is_staff=True,
        )
        url = reverse("admin:accounts_reveluser_changelist")
        admin_client.post(
            url,
            {
                "action": "ban_selected_users",
                "_selected_action": [str(staff.pk)],
            },
        )
        assert not GlobalBan.objects.filter(
            ban_type=GlobalBan.BanType.EMAIL,
            normalized_value=staff.email,
        ).exists()

    def test_skip_already_banned_user(
        self, admin_client: Client, target_user: RevelUser, admin_user: RevelUser
    ) -> None:
        from django.urls import reverse

        # Pre-create the ban
        GlobalBan.objects.create(
            ban_type=GlobalBan.BanType.EMAIL,
            value=target_user.email,
            created_by=admin_user,
        )
        url = reverse("admin:accounts_reveluser_changelist")
        response = admin_client.post(
            url,
            {
                "action": "ban_selected_users",
                "_selected_action": [str(target_user.pk)],
            },
        )
        assert response.status_code == 302
        # Should still be only 1 ban (no duplicate)
        assert (
            GlobalBan.objects.filter(
                ban_type=GlobalBan.BanType.EMAIL,
                normalized_value=target_user.email,
            ).count()
            == 1
        )
