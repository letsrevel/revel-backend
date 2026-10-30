"""Django admin: guest is read-only; the "Convert guest" action goes through convert_guest (#1253)."""

import typing as t
from unittest.mock import patch

import pytest
from django.test import Client, override_settings
from django.urls import reverse

from accounts.models import RevelUser
from notifications.models import NotificationPreference, get_default_notification_type_settings

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("no_manifest_storage")]

ENROLL = "accounts.service.account.referral_application_service.try_enroll_invitee"


@pytest.fixture
def no_manifest_storage() -> t.Iterator[None]:
    """Rendering a full admin page needs a staticfiles manifest for whitenoise; use the plain storage."""
    with override_settings(
        STORAGES={
            "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
            "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
        }
    ):
        yield


@pytest.fixture
def admin_client_(superuser: RevelUser) -> Client:
    client = Client()
    client.force_login(superuser)
    return client


def _run_action(client: Client, users: list[RevelUser]) -> t.Any:
    return client.post(
        reverse("admin:accounts_reveluser_changelist"),
        {"action": "convert_guests", "_selected_action": [str(u.pk) for u in users]},
        follow=True,
    )


def _change_form_post_data(client: Client, user: RevelUser, **overrides: t.Any) -> dict[str, t.Any]:
    """Build a valid POST body from the real change form, as the browser would submit it."""
    response = client.get(reverse("admin:accounts_reveluser_change", args=[user.pk]))
    assert response.status_code == 200
    form = response.context["adminform"].form
    data: dict[str, t.Any] = {}
    for name, field in form.fields.items():
        value = form.initial.get(name)
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            if value:
                data[name] = "on"
        elif isinstance(value, (list, tuple, set)):
            data[name] = [str(getattr(v, "pk", v)) for v in value]
        else:
            data[name] = field.widget.format_value(value) if hasattr(field.widget, "format_value") else value
    for formset in response.context["inline_admin_formsets"]:
        prefix = formset.formset.prefix
        data.update({f"{prefix}-TOTAL_FORMS": 0, f"{prefix}-INITIAL_FORMS": 0, f"{prefix}-MIN_NUM_FORMS": 0})
        data[f"{prefix}-MAX_NUM_FORMS"] = 1000
    data.update(overrides)
    return data


def test_action_converts_a_guest(admin_client_: Client, guest_user: RevelUser) -> None:
    with patch(ENROLL) as enroll:
        _run_action(admin_client_, [guest_user])

    guest_user.refresh_from_db()
    assert (guest_user.guest, guest_user.email_verified) == (False, True)
    enroll.assert_called_once()
    prefs = NotificationPreference.objects.get(user=guest_user).notification_type_settings
    assert dict(prefs) == get_default_notification_type_settings()


def test_action_on_a_non_guest_is_a_noop(admin_client_: Client, user: RevelUser) -> None:
    before = dict(NotificationPreference.objects.get(user=user).notification_type_settings)
    with patch(ENROLL) as enroll:
        _run_action(admin_client_, [user])

    enroll.assert_not_called()
    assert dict(NotificationPreference.objects.get(user=user).notification_type_settings) == before


def test_editing_a_guest_through_the_change_form_never_changes_guest(
    admin_client_: Client, guest_user: RevelUser
) -> None:
    data = _change_form_post_data(admin_client_, guest_user, first_name="Edited")

    response = admin_client_.post(reverse("admin:accounts_reveluser_change", args=[guest_user.pk]), data)

    assert response.status_code == 302, getattr(response, "context", None) and response.context["adminform"].errors
    guest_user.refresh_from_db()
    assert (guest_user.guest, guest_user.first_name) == (True, "Edited")


def test_stale_change_form_never_writes_guest_back(admin_client_: Client, guest_user: RevelUser) -> None:
    """The page was loaded while the user was a guest; the user is converted before the POST."""
    data = _change_form_post_data(admin_client_, guest_user, first_name="Edited")
    with patch(ENROLL):
        _run_action(admin_client_, [guest_user])

    response = admin_client_.post(reverse("admin:accounts_reveluser_change", args=[guest_user.pk]), data)

    assert response.status_code == 302
    guest_user.refresh_from_db()
    assert (guest_user.guest, guest_user.first_name) == (False, "Edited")
