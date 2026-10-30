"""Django admin: guest is read-only; the "Convert guest" action goes through convert_guest (#1253)."""

import typing as t
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse

from accounts.models import GlobalBan, RevelUser
from notifications.models import NotificationPreference, get_default_notification_type_settings

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("no_manifest_storage")]

ENROLL = "accounts.service.account.referral_application_service.try_enroll_invitee"


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


def test_action_converts_a_guest(admin_client: Client, guest_user: RevelUser) -> None:
    with patch(ENROLL) as enroll:
        _run_action(admin_client, [guest_user])

    guest_user.refresh_from_db()
    assert (guest_user.guest, guest_user.email_verified) == (False, True)
    enroll.assert_called_once()
    prefs = NotificationPreference.objects.get(user=guest_user).notification_type_settings
    assert dict(prefs) == get_default_notification_type_settings()


def test_action_on_a_non_guest_is_a_noop(admin_client: Client, user: RevelUser) -> None:
    before = dict(NotificationPreference.objects.get(user=user).notification_type_settings)
    with patch(ENROLL) as enroll:
        _run_action(admin_client, [user])

    enroll.assert_not_called()
    assert dict(NotificationPreference.objects.get(user=user).notification_type_settings) == before


def test_editing_a_guest_through_the_change_form_never_changes_guest(
    admin_client: Client, guest_user: RevelUser
) -> None:
    data = _change_form_post_data(admin_client, guest_user, first_name="Edited")

    response = admin_client.post(reverse("admin:accounts_reveluser_change", args=[guest_user.pk]), data)

    assert response.status_code == 302, getattr(response, "context", None) and response.context["adminform"].errors
    guest_user.refresh_from_db()
    assert (guest_user.guest, guest_user.first_name) == (True, "Edited")


def test_stale_change_form_never_writes_guest_back(admin_client: Client, guest_user: RevelUser) -> None:
    """The page was loaded while the user was a guest; the user is converted before the POST."""
    data = _change_form_post_data(admin_client, guest_user, first_name="Edited")
    with patch(ENROLL):
        _run_action(admin_client, [guest_user])

    response = admin_client.post(reverse("admin:accounts_reveluser_change", args=[guest_user.pk]), data)

    assert response.status_code == 302
    guest_user.refresh_from_db()
    assert (guest_user.guest, guest_user.first_name) == (False, "Edited")


def test_view_only_staff_cannot_run_the_action(guest_user: RevelUser, django_user_model: t.Type[RevelUser]) -> None:
    from django.contrib.auth.models import Permission

    viewer = django_user_model.objects.create_user(
        username="viewer@example.com", email="viewer@example.com", password="strong-password-123!", is_staff=True
    )
    viewer.user_permissions.add(Permission.objects.get(codename="view_reveluser"))
    client = Client()
    client.force_login(viewer)
    url = reverse("admin:accounts_reveluser_changelist")

    assert "convert_guests" not in client.get(url).content.decode()
    with patch(ENROLL) as enroll:
        client.post(url, {"action": "convert_guests", "_selected_action": [str(guest_user.pk)]})

    enroll.assert_not_called()
    guest_user.refresh_from_db()
    assert guest_user.guest is True


def test_action_skips_a_banned_guest_and_warns(admin_client: Client, guest_user: RevelUser) -> None:
    GlobalBan.objects.create(ban_type=GlobalBan.BanType.EMAIL, value=guest_user.email)

    with patch(ENROLL) as enroll:
        response = _run_action(admin_client, [guest_user])

    enroll.assert_not_called()
    guest_user.refresh_from_db()
    assert guest_user.guest is True
    assert "globally banned" in response.content.decode()
