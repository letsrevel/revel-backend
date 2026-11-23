"""The superuser-only admin action that fully deletes an erased account before retention ends (#1476)."""

import typing as t
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Permission
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from accounts.tests.test_convert_guest_admin import _change_form_post_data

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("no_manifest_storage")]

ACTION = "force_full_delete"
CHANGELIST = "admin:accounts_reveluser_changelist"


@pytest.fixture
def erased(user: RevelUser) -> RevelUser:
    RevelUser.objects.filter(pk=user.pk).update(erased_at=timezone.now(), is_active=False)
    return user


def _post(client: Client, users: list[RevelUser], **extra: str) -> t.Any:
    data = {"action": ACTION, "_selected_action": [str(u.pk) for u in users], **extra}
    return client.post(reverse(CHANGELIST), data)


def test_the_first_post_shows_a_confirmation_page_and_queues_nothing(
    admin_client: Client, erased: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    # The dispatch is an on-commit callback: run the callbacks, or "not called" proves nothing.
    with patch("accounts.tasks.gdpr.delete_user_account") as task, django_capture_on_commit_callbacks(execute=True):
        response = _post(admin_client, [erased])

    assert response.status_code == 200
    assert b"confirm_force_full_delete" in response.content
    assert str(erased.pk).encode() in response.content
    task.delay.assert_not_called()


def test_the_confirmed_post_queues_the_full_delete_after_commit_and_logs_ids_only(
    admin_client: Client,
    admin_user: RevelUser,
    erased: RevelUser,
    member: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    with (
        patch("accounts.tasks.gdpr.delete_user_account") as task,
        patch("accounts.admin.user.logger") as log,
        django_capture_on_commit_callbacks(execute=True),
    ):
        _post(admin_client, [erased, member], confirm_force_full_delete="yes")
        task.delay.assert_not_called()  # queued on commit, not in the request transaction

    task.delay.assert_called_once_with(str(erased.pk), force_full_delete=True)  # the live account is skipped
    log.warning.assert_called_once_with(
        "account_force_full_delete_queued", actor_id=str(admin_user.pk), user_id=str(erased.pk)
    )


def test_staff_who_are_not_superusers_cannot_run_it(
    django_user_model: type[RevelUser], erased: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    staff = django_user_model.objects.create_user(
        username="staff@example.com", email="staff@example.com", password="strong-password-123!", is_staff=True
    )
    staff.user_permissions.add(*Permission.objects.filter(codename__in=["view_reveluser", "change_reveluser"]))
    client = Client()
    client.force_login(staff)

    assert ACTION not in client.get(reverse(CHANGELIST)).content.decode()
    with patch("accounts.tasks.gdpr.delete_user_account") as task, django_capture_on_commit_callbacks(execute=True):
        _post(client, [erased], confirm_force_full_delete="yes")

    task.delay.assert_not_called()


def test_the_changelist_filters_erased_accounts(admin_client: Client, erased: RevelUser, member: RevelUser) -> None:
    response = admin_client.get(reverse(CHANGELIST), {"erased_at__isempty": "0"})

    assert response.status_code == 200
    content = response.content.decode()
    assert str(erased.pk) in content
    assert str(member.pk) not in content


@pytest.fixture
def member(django_user_model: type[RevelUser]) -> RevelUser:
    """A live account that is not erased."""
    return django_user_model.objects.create_user(username="live@example.com", email="live@example.com")


def test_the_confirmed_post_clears_the_self_service_deletion_mark(
    admin_client: Client, erased: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    """An admin run never gives access back, so the force delete removes the mark too."""
    RevelUser.objects.filter(pk=erased.pk).update(deletion_requested_at=timezone.now())
    with patch("accounts.tasks.gdpr.delete_user_account"), django_capture_on_commit_callbacks(execute=True):
        _post(admin_client, [erased], confirm_force_full_delete="yes")

    erased.refresh_from_db()
    assert erased.deletion_requested_at is None


def test_the_change_form_carries_the_shown_is_active(admin_client: Client, member: RevelUser) -> None:
    """The admin compares ``is_active`` with this hidden value (see ``RevelUserAdmin.get_form``)."""
    response = admin_client.get(reverse("admin:accounts_reveluser_change", args=[member.pk]))

    assert response.status_code == 200
    assert b'name="initial-is_active"' in response.content


def test_the_add_user_page_works(admin_client: Client) -> None:
    """The add form has no ``is_active``, and a new row has nothing stored to keep (see ``save_model``)."""
    url = reverse("admin:accounts_reveluser_add")
    page = admin_client.get(url)
    assert page.status_code == 200
    data = {
        "username": "new@example.com",
        "usable_password": "true",
        "password1": "A-strong-password-123!",
        "password2": "A-strong-password-123!",
    }
    for inline in page.context["inline_admin_formsets"]:  # empty inlines, as the browser posts them
        prefix = inline.formset.prefix
        data |= {f"{prefix}-TOTAL_FORMS": "0", f"{prefix}-INITIAL_FORMS": "0"}

    response = admin_client.post(url, data)

    assert response.status_code == 302
    assert RevelUser.objects.filter(username="new@example.com").exists()


class TestChangeFormOfAnErasedAccount:
    """A change page opened before the erasure still holds the old personal data; its post must not save it."""

    @pytest.fixture
    def person(self, django_user_model: type[RevelUser]) -> RevelUser:
        return django_user_model.objects.create_user(
            username="ada@example.com", email="ada@example.com", first_name="Ada", last_name="Lovelace", bio="Hi"
        )

    @staticmethod
    def _erase(user: RevelUser) -> None:
        from accounts.service.account_erasure import erase_account_keeping_holdings
        from events.service.account_deletion_service import KeptHoldings

        erase_account_keeping_holdings(user, KeptHoldings(ticket_ids=frozenset(), pass_ids=frozenset()))

    def test_a_page_opened_before_the_erasure_is_refused_and_writes_nothing_back(
        self, admin_client: Client, person: RevelUser
    ) -> None:
        url = reverse("admin:accounts_reveluser_change", args=[person.pk])
        data = _change_form_post_data(admin_client, person, bio="Edited")
        self._erase(person)

        response = admin_client.post(url, data)

        assert response.status_code == 200  # the form again, with the error
        assert "An erased account cannot be edited." in response.content.decode()
        erased = RevelUser.objects.get(pk=person.pk)
        assert erased.erased_at is not None
        assert "ada@example.com" not in (erased.email, erased.username)
        assert (erased.first_name, erased.last_name, erased.bio) == ("", "", "")

    def test_a_post_whose_account_was_deleted_meanwhile_is_refused(self, person: RevelUser) -> None:
        """The view loaded the account; a full delete commits while the post waits for the row lock."""
        from django.contrib import admin as django_admin

        from accounts.admin.user import RevelUserAdmin

        model_admin = RevelUserAdmin(RevelUser, django_admin.site)
        request = type("Request", (), {"user": person})()
        loaded = RevelUser.objects.get(pk=person.pk)
        form_class = model_admin.get_form(request, loaded, change=True, fields=["bio"])
        RevelUser.objects.filter(pk=person.pk).delete()

        form = form_class({"bio": "Edited"}, instance=loaded)

        assert not form.is_valid()
        assert form.non_field_errors() == ["This account was deleted."]

    def test_an_account_that_is_not_erased_still_saves(self, admin_client: Client, person: RevelUser) -> None:
        url = reverse("admin:accounts_reveluser_change", args=[person.pk])
        data = _change_form_post_data(admin_client, person, bio="Edited")

        response = admin_client.post(url, data)

        assert response.status_code == 302
        assert RevelUser.objects.get(pk=person.pk).bio == "Edited"


class TestPasswordChangeOfAnErasedAccount:
    """The admin password change loads the account before the post is saved; an erasure can commit in between."""

    NEW_PASSWORD = "A-new-strong-password-123!"

    @pytest.fixture
    def person(self, django_user_model: type[RevelUser]) -> RevelUser:
        return django_user_model.objects.create_user(
            username="ada@example.com",
            email="ada@example.com",
            password="An-old-password-456!",
            first_name="Ada",
            bio="Hi",
        )

    def _post_after(self, client: Client, person: RevelUser, between: t.Callable[[], object]) -> t.Any:
        """Open the page, then post it; ``between`` runs after the view has loaded the account."""
        from accounts.admin.user import RevelUserAdmin

        url = reverse("admin:auth_user_password_change", args=[person.pk])
        assert client.get(url).status_code == 200
        load = RevelUserAdmin.get_object

        def load_then_change(model_admin: RevelUserAdmin, *args: t.Any, **kwargs: t.Any) -> RevelUser | None:
            loaded = load(model_admin, *args, **kwargs)
            between()
            return loaded

        data = {"usable_password": "true", "password1": self.NEW_PASSWORD, "password2": self.NEW_PASSWORD}
        with patch.object(RevelUserAdmin, "get_object", load_then_change):
            return client.post(url, data)

    def test_an_erasure_that_commits_after_the_load_is_not_undone(
        self, admin_client: Client, person: RevelUser
    ) -> None:
        response = self._post_after(admin_client, person, lambda: TestChangeFormOfAnErasedAccount._erase(person))

        assert response.status_code == 200  # the form again, with the error
        assert "An erased account cannot be edited." in response.content.decode()
        erased = RevelUser.objects.get(pk=person.pk)
        assert erased.erased_at is not None
        assert "ada@example.com" not in (erased.email, erased.username)
        assert (erased.is_active, erased.first_name, erased.bio) == (False, "", "")
        assert not erased.has_usable_password()

    def test_a_post_whose_account_was_deleted_meanwhile_is_refused(
        self, admin_client: Client, person: RevelUser
    ) -> None:
        response = self._post_after(admin_client, person, lambda: RevelUser.objects.filter(pk=person.pk).delete())

        assert response.status_code == 200
        assert "This account was deleted." in response.content.decode()
        assert not RevelUser.objects.filter(pk=person.pk).exists()

    def test_a_normal_change_sets_only_the_password(self, admin_client: Client, person: RevelUser) -> None:
        """An edit that commits after the load is kept: the stale instance writes back no other field."""
        response = self._post_after(
            admin_client, person, lambda: RevelUser.objects.filter(pk=person.pk).update(bio="Edited meanwhile")
        )

        assert response.status_code == 302
        changed = RevelUser.objects.get(pk=person.pk)
        assert changed.check_password(self.NEW_PASSWORD)
        assert (changed.bio, changed.email, changed.erased_at) == ("Edited meanwhile", "ada@example.com", None)

    def test_disabling_password_sign_in_still_works(self, admin_client: Client, person: RevelUser) -> None:
        url = reverse("admin:auth_user_password_change", args=[person.pk])

        response = admin_client.post(url, {"usable_password": "false", "unset-password": "1"})

        assert response.status_code == 302
        assert not RevelUser.objects.get(pk=person.pk).has_usable_password()
