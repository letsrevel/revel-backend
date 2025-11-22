"""Tests for email verification reminder system."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone
from freezegun import freeze_time

from accounts.models import EmailVerificationReminderTracking, RevelUser
from accounts.service.account import verify_email
from accounts.tasks import (
    deactivate_unverified_accounts,
    delete_old_inactive_accounts,
    send_early_verification_reminders,
    send_final_verification_warnings,
)


@pytest.fixture
def old_unverified_user_24h(django_user_model: type[RevelUser]) -> RevelUser:
    """User created 25 hours ago, unverified."""
    with freeze_time(timezone.now() - timedelta(hours=25)):
        return django_user_model.objects.create_user(
            username="user24h@example.com",
            email="user24h@example.com",
            password="password123",
            email_verified=False,
        )


@pytest.fixture
def old_unverified_user_4d(django_user_model: type[RevelUser]) -> RevelUser:
    """User created 4 days ago, unverified."""
    with freeze_time(timezone.now() - timedelta(days=4)):
        return django_user_model.objects.create_user(
            username="user4d@example.com",
            email="user4d@example.com",
            password="password123",
            email_verified=False,
        )


@pytest.fixture
def old_unverified_user_8d(django_user_model: type[RevelUser]) -> RevelUser:
    """User created 8 days ago, unverified."""
    with freeze_time(timezone.now() - timedelta(days=8)):
        return django_user_model.objects.create_user(
            username="user8d@example.com",
            email="user8d@example.com",
            password="password123",
            email_verified=False,
        )


@pytest.fixture
def old_unverified_user_31d(django_user_model: type[RevelUser]) -> RevelUser:
    """User created 31 days ago, unverified."""
    with freeze_time(timezone.now() - timedelta(days=31)):
        return django_user_model.objects.create_user(
            username="user31d@example.com",
            email="user31d@example.com",
            password="password123",
            email_verified=False,
        )


# Test Early Reminders


@pytest.mark.django_db
@patch("accounts.tasks.send_email.delay")
def test_send_early_reminder_24h_never_sent(
    mock_send_email: MagicMock,
    old_unverified_user_24h: RevelUser,
) -> None:
    """Test that 24h reminder is sent if never sent before."""
    result = send_early_verification_reminders()
    assert result["24h"] == 1
    assert result["3d"] == 0
    assert result["7d"] == 0
    assert mock_send_email.call_count == 1
    assert EmailVerificationReminderTracking.objects.filter(user=old_unverified_user_24h).exists()


@pytest.mark.django_db
@patch("accounts.tasks.send_email.delay")
def test_send_early_reminder_24h_recently_sent(
    mock_send_email: MagicMock,
    old_unverified_user_24h: RevelUser,
) -> None:
    """Test that 24h reminder is NOT sent if sent within last 24 hours."""
    # Create tracking with recent reminder
    EmailVerificationReminderTracking.objects.create(
        user=old_unverified_user_24h,
        last_reminder_sent_at=timezone.now() - timedelta(hours=12),
    )
    result = send_early_verification_reminders()
    assert result["24h"] == 0
    assert mock_send_email.call_count == 0


@pytest.mark.django_db
@patch("accounts.tasks.send_email.delay")
def test_send_early_reminder_24h_old_reminder(
    mock_send_email: MagicMock,
    old_unverified_user_24h: RevelUser,
) -> None:
    """Test that 24h reminder IS sent if last reminder was > 24h ago."""
    # Create tracking with old reminder
    EmailVerificationReminderTracking.objects.create(
        user=old_unverified_user_24h,
        last_reminder_sent_at=timezone.now() - timedelta(hours=26),
    )
    result = send_early_verification_reminders()
    assert result["24h"] == 1
    assert mock_send_email.call_count == 1


@pytest.mark.django_db
@patch("accounts.tasks.send_email.delay")
def test_send_early_reminder_3d_48h_passed(
    mock_send_email: MagicMock,
    old_unverified_user_4d: RevelUser,
) -> None:
    """Test that 3d reminder is sent if 48h passed since last reminder."""
    # Create tracking with reminder sent 3 days ago
    EmailVerificationReminderTracking.objects.create(
        user=old_unverified_user_4d,
        last_reminder_sent_at=timezone.now() - timedelta(days=3),
    )
    result = send_early_verification_reminders()
    assert result["3d"] == 1
    assert mock_send_email.call_count == 1


@pytest.mark.django_db
@patch("accounts.tasks.send_email.delay")
def test_send_early_reminder_3d_only_24h_passed(
    mock_send_email: MagicMock,
    old_unverified_user_4d: RevelUser,
) -> None:
    """Test that 3d reminder is NOT sent if only 24h passed."""
    # Create tracking with reminder sent 1 day ago
    EmailVerificationReminderTracking.objects.create(
        user=old_unverified_user_4d,
        last_reminder_sent_at=timezone.now() - timedelta(days=1),
    )
    result = send_early_verification_reminders()
    assert result["3d"] == 0
    assert mock_send_email.call_count == 0


@pytest.mark.django_db
@patch("accounts.tasks.send_email.delay")
def test_send_early_reminder_7d_week_passed(
    mock_send_email: MagicMock,
    old_unverified_user_8d: RevelUser,
) -> None:
    """Test that 7d reminder is sent if 7 days passed since last reminder."""
    # Create tracking with reminder sent 8 days ago
    EmailVerificationReminderTracking.objects.create(
        user=old_unverified_user_8d,
        last_reminder_sent_at=timezone.now() - timedelta(days=8),
    )
    result = send_early_verification_reminders()
    assert result["7d"] == 1
    assert mock_send_email.call_count == 1


@pytest.mark.django_db
@patch("accounts.tasks.send_email.delay")
def test_send_early_reminder_skips_if_final_warning_sent(
    mock_send_email: MagicMock,
    old_unverified_user_24h: RevelUser,
) -> None:
    """Test that early reminders are skipped if final warning already sent."""
    EmailVerificationReminderTracking.objects.create(
        user=old_unverified_user_24h,
        final_warning_sent_at=timezone.now() - timedelta(days=1),
    )
    result = send_early_verification_reminders()
    assert result["24h"] == 0
    assert mock_send_email.call_count == 0


@pytest.mark.django_db
@patch("accounts.tasks.send_email.delay")
def test_send_early_reminder_skips_verified_users(
    mock_send_email: MagicMock,
    old_unverified_user_24h: RevelUser,
) -> None:
    """Test that verified users don't receive reminders."""
    old_unverified_user_24h.email_verified = True
    old_unverified_user_24h.save()
    result = send_early_verification_reminders()
    assert result["24h"] == 0
    assert mock_send_email.call_count == 0


@pytest.mark.django_db
@patch("accounts.tasks.send_email.delay")
def test_send_early_reminder_skips_guest_users(
    mock_send_email: MagicMock,
    old_unverified_user_24h: RevelUser,
) -> None:
    """Test that guest users don't receive reminders."""
    old_unverified_user_24h.guest = True
    old_unverified_user_24h.save()
    result = send_early_verification_reminders()
    assert result["24h"] == 0
    assert mock_send_email.call_count == 0


@pytest.mark.django_db
@patch("accounts.tasks.send_email.delay")
def test_send_early_reminder_skips_inactive_users(
    mock_send_email: MagicMock,
    old_unverified_user_24h: RevelUser,
) -> None:
    """Test that inactive users don't receive early reminders."""
    old_unverified_user_24h.is_active = False
    old_unverified_user_24h.save()
    result = send_early_verification_reminders()
    assert result["24h"] == 0
    assert mock_send_email.call_count == 0


# Test Final Warnings


@pytest.mark.django_db
@patch("accounts.tasks.send_email.delay")
def test_send_final_warning_30d_old(
    mock_send_email: MagicMock,
    old_unverified_user_31d: RevelUser,
) -> None:
    """Test that final warning is sent for 30+ day old unverified accounts."""
    result = send_final_verification_warnings()
    assert result["count"] == 1
    assert mock_send_email.call_count == 1

    # Note: tracking.final_warning_sent_at is now updated in the callback,
    # which doesn't execute when send_email.delay is mocked


@pytest.mark.django_db
@patch("accounts.tasks.send_email.delay")
def test_send_final_warning_only_once(
    mock_send_email: MagicMock,
    old_unverified_user_31d: RevelUser,
) -> None:
    """Test that final warning is only sent once."""
    # Create tracking with final warning already sent
    EmailVerificationReminderTracking.objects.create(
        user=old_unverified_user_31d,
        final_warning_sent_at=timezone.now() - timedelta(days=1),
    )
    result = send_final_verification_warnings()
    assert result["count"] == 0
    assert mock_send_email.call_count == 0


@pytest.mark.django_db
@patch("accounts.tasks.send_email.delay")
def test_send_final_warning_skips_verified_users(
    mock_send_email: MagicMock,
    old_unverified_user_31d: RevelUser,
) -> None:
    """Test that verified users don't receive final warnings."""
    old_unverified_user_31d.email_verified = True
    old_unverified_user_31d.save()
    result = send_final_verification_warnings()
    assert result["count"] == 0
    assert mock_send_email.call_count == 0


# Test Deactivation


@pytest.mark.django_db
@patch("accounts.tasks.send_email.delay")
def test_deactivate_unverified_accounts(
    mock_send_email: MagicMock,
    old_unverified_user_31d: RevelUser,
) -> None:
    """Test that accounts with final warning are deactivated."""
    # Create tracking with final warning sent
    EmailVerificationReminderTracking.objects.create(
        user=old_unverified_user_31d,
        final_warning_sent_at=timezone.now() - timedelta(days=1),
    )
    result = deactivate_unverified_accounts()
    assert result["count"] == 1
    assert mock_send_email.call_count == 1

    # Check user was deactivated
    old_unverified_user_31d.refresh_from_db()
    assert not old_unverified_user_31d.is_active

    # Note: tracking.deactivation_email_sent_at is now updated in the callback,
    # which doesn't execute when send_email.delay is mocked


@pytest.mark.django_db
@patch("accounts.tasks.send_email.delay")
def test_deactivate_only_once(
    mock_send_email: MagicMock,
    old_unverified_user_31d: RevelUser,
) -> None:
    """Test that deactivation email is only sent once."""
    # User already deactivated
    old_unverified_user_31d.is_active = False
    old_unverified_user_31d.save()

    EmailVerificationReminderTracking.objects.create(
        user=old_unverified_user_31d,
        final_warning_sent_at=timezone.now() - timedelta(days=1),
        deactivation_email_sent_at=timezone.now() - timedelta(hours=1),
    )
    result = deactivate_unverified_accounts()
    assert result["count"] == 0
    assert mock_send_email.call_count == 0


@pytest.mark.django_db
@patch("accounts.tasks.send_email.delay")
def test_deactivate_skips_verified_users(
    mock_send_email: MagicMock,
    old_unverified_user_31d: RevelUser,
) -> None:
    """Test that verified users are not deactivated."""
    old_unverified_user_31d.email_verified = True
    old_unverified_user_31d.save()

    EmailVerificationReminderTracking.objects.create(
        user=old_unverified_user_31d,
        final_warning_sent_at=timezone.now() - timedelta(days=1),
    )
    result = deactivate_unverified_accounts()
    assert result["count"] == 0
    assert mock_send_email.call_count == 0


# Test Deletion


@pytest.mark.django_db
def test_delete_old_inactive_accounts_61d(
    old_unverified_user_31d: RevelUser,
) -> None:
    """Test that accounts deactivated 60+ days ago are deleted."""
    # Deactivate user
    old_unverified_user_31d.is_active = False
    old_unverified_user_31d.save()

    # Create tracking with old deactivation
    EmailVerificationReminderTracking.objects.create(
        user=old_unverified_user_31d,
        deactivation_email_sent_at=timezone.now() - timedelta(days=61),
    )

    result = delete_old_inactive_accounts()
    assert result["count"] == 1

    # Check user was deleted
    assert not RevelUser.objects.filter(id=old_unverified_user_31d.id).exists()
    # Tracking should be cascade deleted
    assert not EmailVerificationReminderTracking.objects.filter(user=old_unverified_user_31d).exists()


@pytest.mark.django_db
def test_delete_old_inactive_accounts_59d_too_early(
    old_unverified_user_31d: RevelUser,
) -> None:
    """Test that accounts deactivated < 60 days ago are NOT deleted."""
    # Deactivate user
    old_unverified_user_31d.is_active = False
    old_unverified_user_31d.save()

    # Create tracking with recent deactivation (59 days)
    EmailVerificationReminderTracking.objects.create(
        user=old_unverified_user_31d,
        deactivation_email_sent_at=timezone.now() - timedelta(days=59),
    )

    result = delete_old_inactive_accounts()
    assert result["count"] == 0

    # User still exists
    assert RevelUser.objects.filter(id=old_unverified_user_31d.id).exists()


@pytest.mark.django_db
def test_delete_skips_active_users(
    old_unverified_user_31d: RevelUser,
) -> None:
    """Test that active users are not deleted."""
    # User is active
    assert old_unverified_user_31d.is_active

    # Create tracking with old deactivation (shouldn't happen but testing edge case)
    EmailVerificationReminderTracking.objects.create(
        user=old_unverified_user_31d,
        deactivation_email_sent_at=timezone.now() - timedelta(days=61),
    )

    result = delete_old_inactive_accounts()
    assert result["count"] == 0
    assert RevelUser.objects.filter(id=old_unverified_user_31d.id).exists()


@pytest.mark.django_db
def test_delete_skips_verified_users(
    old_unverified_user_31d: RevelUser,
) -> None:
    """Test that verified users are not deleted."""
    # Deactivate but verify user (edge case)
    old_unverified_user_31d.is_active = False
    old_unverified_user_31d.email_verified = True
    old_unverified_user_31d.save()

    EmailVerificationReminderTracking.objects.create(
        user=old_unverified_user_31d,
        deactivation_email_sent_at=timezone.now() - timedelta(days=61),
    )

    result = delete_old_inactive_accounts()
    assert result["count"] == 0
    assert RevelUser.objects.filter(id=old_unverified_user_31d.id).exists()


# Test Verification Flow


@pytest.mark.django_db
def test_verify_email_clears_tracking(
    old_unverified_user_24h: RevelUser,
) -> None:
    """Test that verifying email clears reminder tracking."""
    from accounts.service.account import create_verification_token

    # Create tracking
    EmailVerificationReminderTracking.objects.create(
        user=old_unverified_user_24h,
        last_reminder_sent_at=timezone.now() - timedelta(hours=1),
    )

    # Verify email
    token = create_verification_token(old_unverified_user_24h)
    verify_email(token)

    # Check tracking was deleted
    assert not EmailVerificationReminderTracking.objects.filter(user=old_unverified_user_24h).exists()


@pytest.mark.django_db
def test_verify_email_reactivates_deactivated_account(
    old_unverified_user_31d: RevelUser,
) -> None:
    """Test that verifying email reactivates a deactivated account."""
    from accounts.service.account import create_verification_token

    # Deactivate user
    old_unverified_user_31d.is_active = False
    old_unverified_user_31d.save()

    # Create tracking
    EmailVerificationReminderTracking.objects.create(
        user=old_unverified_user_31d,
        final_warning_sent_at=timezone.now() - timedelta(days=2),
        deactivation_email_sent_at=timezone.now() - timedelta(days=1),
    )

    # Verify email
    token = create_verification_token(old_unverified_user_31d)
    user = verify_email(token)

    # Check user was reactivated
    assert user.is_active
    assert user.email_verified

    # Check tracking was deleted
    assert not EmailVerificationReminderTracking.objects.filter(user=old_unverified_user_31d).exists()
