"""``convert_guest``: the single guest -> account conversion path (#1248, #1253)."""

from unittest.mock import MagicMock, patch

import pytest
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
    with patch("accounts.service.account.reset_converted_guest_preferences", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            convert_guest(guest_user)

    guest_user.refresh_from_db()
    assert guest_user.guest is True
    assert guest_user.email_verified is False


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
