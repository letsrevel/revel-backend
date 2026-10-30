"""``convert_guest``: the single guest -> account conversion path (#1248, #1253)."""

import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest
from django.conf import settings
from django.contrib import admin
from django.test import RequestFactory

from accounts.admin.user import RevelUserAdmin
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


def _admin_save(obj: RevelUser, changed: list[str]) -> None:
    model_admin = RevelUserAdmin(RevelUser, admin.site)
    form = MagicMock(changed_data=changed)
    model_admin.save_model(RequestFactory().post("/"), obj, form, change=True)


def test_admin_unticking_guest_goes_through_convert_guest(guest_user: RevelUser) -> None:
    guest_user.first_name = "Edited"
    guest_user.guest = False  # what the admin form does to the instance
    with patch(ENROLL) as enroll:
        _admin_save(guest_user, ["guest", "first_name"])

    guest_user.refresh_from_db()
    assert (guest_user.guest, guest_user.email_verified, guest_user.first_name) == (False, True, "Edited")
    enroll.assert_called_once()
    assert _prefs(guest_user) == get_default_notification_type_settings()


def test_admin_other_edits_do_not_convert(guest_user: RevelUser) -> None:
    before = _prefs(guest_user)
    guest_user.first_name = "Edited"
    with patch(ENROLL) as enroll:
        _admin_save(guest_user, ["first_name"])

    guest_user.refresh_from_db()
    assert (guest_user.guest, guest_user.first_name) == (True, "Edited")
    enroll.assert_not_called()
    assert _prefs(guest_user) == before


def test_admin_edit_of_regular_user_works(user: RevelUser) -> None:
    user.first_name = "Edited"
    _admin_save(user, ["first_name"])

    user.refresh_from_db()
    assert user.first_name == "Edited"


def test_admin_stale_form_does_not_resend_conversion(guest_user: RevelUser) -> None:
    """The guest was converted elsewhere after the admin form loaded: no second conversion, no stale write."""
    stale = RevelUser.objects.get(pk=guest_user.pk)  # form loaded while still a guest
    with patch(ENROLL):
        convert_guest(guest_user)
    prefs_before = _prefs(guest_user)
    stale.guest = False  # the stale form unticks guest
    stale.first_name = "Edited"

    with patch(ENROLL) as enroll, patch("notifications.signals.user.reset_converted_guest_preferences") as reset:
        _admin_save(stale, ["guest", "first_name"])

    enroll.assert_not_called()
    reset.assert_not_called()
    guest_user.refresh_from_db()
    assert (guest_user.guest, guest_user.first_name) == (False, "Edited")
    assert _prefs(guest_user) == prefs_before


def test_admin_stale_form_with_unchanged_guest_does_not_revert_conversion(guest_user: RevelUser) -> None:
    stale = RevelUser.objects.get(pk=guest_user.pk)
    with patch(ENROLL):
        convert_guest(guest_user)
    stale.first_name = "Edited"  # guest still True on the stale instance, not in changed_data

    _admin_save(stale, ["first_name"])

    guest_user.refresh_from_db()
    assert (guest_user.guest, guest_user.first_name) == (False, "Edited")


def test_guest_converted_lives_in_a_leaf_module() -> None:
    """Importing the signal must not pull in the service/task chain (that chain formed an import cycle)."""
    code = (
        "import sys, accounts.dispatch;"
        "bad = [m for m in sys.modules if m.startswith(('accounts.service', 'accounts.signals', 'accounts.tasks'))];"
        "assert not bad, bad"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, cwd=settings.BASE_DIR)
    assert result.returncode == 0, result.stderr
