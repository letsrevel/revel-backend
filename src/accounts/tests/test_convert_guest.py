"""``convert_guest``: the single guest -> account conversion path (#1248, #1253)."""

import subprocess
import sys
from unittest.mock import patch

import pytest
from django.conf import settings

from accounts.models import RevelUser
from accounts.service.account import convert_guest
from notifications.models import NotificationPreference, get_default_notification_type_settings

pytestmark = pytest.mark.django_db

ENROLL = "accounts.service.account.referral_application_service.try_enroll_invitee"


def _prefs(user: RevelUser) -> dict[str, object]:
    return dict(NotificationPreference.objects.get(user=user).notification_type_settings)


def test_convert_guest_does_everything(guest_user: RevelUser) -> None:
    with patch(ENROLL) as enroll:
        convert_guest(guest_user)

    guest_user.refresh_from_db()
    assert (guest_user.guest, guest_user.email_verified) == (False, True)
    enroll.assert_called_once_with(guest_user)
    assert _prefs(guest_user) == get_default_notification_type_settings()


def test_preference_failure_rolls_the_conversion_back(guest_user: RevelUser) -> None:
    with patch("notifications.signals.user.reset_converted_guest_preferences", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            convert_guest(guest_user)

    guest_user.refresh_from_db()
    assert guest_user.guest is True
    assert guest_user.email_verified is False


def test_second_call_is_a_noop_without_duplicate_enrolment(guest_user: RevelUser) -> None:
    with patch(ENROLL) as enroll:
        convert_guest(guest_user)
        stale = RevelUser.objects.get(pk=guest_user.pk)
        stale.guest = True  # a caller holding a stale in-memory copy
        convert_guest(stale)

    enroll.assert_called_once()
    assert stale.guest is False  # fresh DB state is copied back


def test_non_guest_is_a_noop(user: RevelUser) -> None:
    """Documented behaviour: converting a non-guest does nothing (no raise, no side effects)."""
    before = _prefs(user)
    with patch(ENROLL) as enroll:
        convert_guest(user)

    enroll.assert_not_called()
    assert _prefs(user) == before


def test_reactivate_revives_an_inactive_guest(guest_user: RevelUser) -> None:
    RevelUser.objects.filter(pk=guest_user.pk).update(is_active=False)
    guest_user.refresh_from_db()

    convert_guest(guest_user, reactivate=True)

    guest_user.refresh_from_db()
    assert (guest_user.guest, guest_user.is_active) == (False, True)


def test_inactive_guest_stays_inactive_without_reactivate(guest_user: RevelUser) -> None:
    RevelUser.objects.filter(pk=guest_user.pk).update(is_active=False)
    guest_user.refresh_from_db()

    convert_guest(guest_user)

    guest_user.refresh_from_db()
    assert (guest_user.guest, guest_user.is_active) == (False, False)


def test_reactivate_is_ignored_for_a_non_guest(user: RevelUser) -> None:
    RevelUser.objects.filter(pk=user.pk).update(is_active=False)
    user.refresh_from_db()

    convert_guest(user, reactivate=True)

    user.refresh_from_db()
    assert user.is_active is False


def test_guest_converted_lives_in_a_leaf_module() -> None:
    """Importing the signal must not pull in the service/task chain (that chain formed an import cycle)."""
    code = (
        "import sys, accounts.dispatch;"
        "bad = [m for m in sys.modules if m.startswith(('accounts.service', 'accounts.signals', 'accounts.tasks'))];"
        "assert not bad, bad"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, cwd=settings.BASE_DIR)
    assert result.returncode == 0, result.stderr
