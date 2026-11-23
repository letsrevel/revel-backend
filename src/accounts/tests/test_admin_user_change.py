"""The admin change form and the admin password change write only what the admin changed."""

import typing as t
from html.parser import HTMLParser
from unittest.mock import patch

import pytest
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import Group
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.token_blacklist.models import BlacklistedToken
from ninja_jwt.tokens import RefreshToken

from accounts.admin.user import RevelUserAdmin
from accounts.models import RevelUser
from accounts.tests.test_convert_guest_admin import _change_form_post_data

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("no_manifest_storage")]

NEW_PASSWORD = "A-new-strong-password-123!"


@pytest.fixture
def person(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="ada@example.com", email="ada@example.com", password="An-old-password-456!", first_name="Ada"
    )


def _with_write_after_load(between: t.Callable[[], object]) -> t.Any:
    """Patch the admin so that ``between`` runs after the view has loaded the account and before the save."""
    load = RevelUserAdmin.get_object

    def load_then_change(model_admin: RevelUserAdmin, *args: t.Any, **kwargs: t.Any) -> RevelUser | None:
        loaded = load(model_admin, *args, **kwargs)
        between()
        return loaded

    return patch.object(RevelUserAdmin, "get_object", load_then_change)


class _ShownValues(HTMLParser):
    """Collect the hidden ``initial-*`` inputs of a rendered change page."""

    def __init__(self) -> None:
        super().__init__()
        self.values: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = dict(attrs)
        name = found.get("name") or ""
        if tag == "input" and name.startswith("initial-"):
            self.values.setdefault(name, []).append(found.get("value") or "")


def _browser_post_data(client: Client, user: RevelUser, **overrides: t.Any) -> dict[str, t.Any]:
    """The post of a browser: the form values plus the values that the rendered page showed."""
    data = _change_form_post_data(client, user, **overrides)
    parser = _ShownValues()
    parser.feed(client.get(reverse("admin:accounts_reveluser_change", args=[user.pk])).content.decode())
    assert "initial-totp_active" in parser.values
    return data | parser.values


class TestPasswordChange:
    def test_it_sets_credentials_changed_at_and_ends_the_sessions(
        self, admin_client: Client, person: RevelUser
    ) -> None:
        RefreshToken.for_user(person)
        before = timezone.now()

        response = admin_client.post(
            reverse("admin:auth_user_password_change", args=[person.pk]),
            {"usable_password": "true", "password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
        )

        assert response.status_code == 302
        changed = RevelUser.objects.get(pk=person.pk)
        assert changed.check_password(NEW_PASSWORD)
        assert changed.credentials_changed_at is not None
        assert changed.credentials_changed_at >= before
        assert BlacklistedToken.objects.filter(token__user=person).count() == 1

    def test_disabling_password_sign_in_also_ends_the_sessions(self, admin_client: Client, person: RevelUser) -> None:
        RefreshToken.for_user(person)

        response = admin_client.post(
            reverse("admin:auth_user_password_change", args=[person.pk]),
            {"usable_password": "false", "unset-password": "1"},
        )

        assert response.status_code == 302
        changed = RevelUser.objects.get(pk=person.pk)
        assert not changed.has_usable_password()
        assert changed.credentials_changed_at is not None
        assert BlacklistedToken.objects.filter(token__user=person).count() == 1


class TestChangeForm:
    def test_it_writes_the_changed_fields(self, admin_client: Client, person: RevelUser) -> None:
        data = _change_form_post_data(admin_client, person, first_name="Augusta", bio="Edited")

        response = admin_client.post(reverse("admin:accounts_reveluser_change", args=[person.pk]), data)

        assert response.status_code == 302
        assert RevelUser.objects.values_list("first_name", "bio").get(pk=person.pk) == ("Augusta", "Edited")

    def test_a_write_after_the_load_to_a_field_that_the_admin_did_not_change_is_kept(
        self, admin_client: Client, person: RevelUser
    ) -> None:
        """A password reset commits after the view loaded the account; the admin edits the bio only."""
        reset_at = timezone.now()
        new_hash = make_password(NEW_PASSWORD)
        data = _change_form_post_data(admin_client, person, bio="Edited")

        def reset() -> None:
            RevelUser.objects.filter(pk=person.pk).update(
                password=new_hash, credentials_changed_at=reset_at, totp_active=True, preferred_name="Countess"
            )

        with _with_write_after_load(reset):
            response = admin_client.post(reverse("admin:accounts_reveluser_change", args=[person.pk]), data)

        assert response.status_code == 302
        changed = RevelUser.objects.get(pk=person.pk)
        assert changed.bio == "Edited"
        assert changed.check_password(NEW_PASSWORD)
        assert changed.credentials_changed_at == reset_at
        assert changed.totp_active is True  # in the form, not changed by the admin
        assert changed.preferred_name == "Countess"

    def test_a_post_without_changes_writes_nothing(self, admin_client: Client, person: RevelUser) -> None:
        data = _change_form_post_data(admin_client, person)

        with _with_write_after_load(lambda: RevelUser.objects.filter(pk=person.pk).update(bio="Meanwhile")):
            response = admin_client.post(reverse("admin:accounts_reveluser_change", args=[person.pk]), data)

        assert response.status_code == 302
        assert RevelUser.objects.get(pk=person.pk).bio == "Meanwhile"

    @pytest.mark.parametrize("browser", [True, False], ids=["with-shown-value", "scripted-client"])
    def test_a_deactivation_still_clears_the_deletion_mark(
        self, admin_client: Client, person: RevelUser, browser: bool
    ) -> None:
        RevelUser.objects.filter(pk=person.pk).update(deletion_requested_at=timezone.now())
        data = _change_form_post_data(admin_client, person)
        del data["is_active"]
        if browser:
            data["initial-is_active"] = "True"  # the hidden value that the page showed

        response = admin_client.post(reverse("admin:accounts_reveluser_change", args=[person.pk]), data)

        assert response.status_code == 302
        changed = RevelUser.objects.get(pk=person.pk)
        assert (changed.is_active, changed.deletion_requested_at) == (False, None)


class TestChangeFormOpenedBeforeAChange:
    """The database changes after the page was rendered and before the admin posts it."""

    @pytest.fixture
    def group(self, person: RevelUser) -> Group:
        group = Group.objects.create(name="Helpers")
        person.groups.add(group)
        return group

    def test_fields_and_relations_that_the_admin_did_not_change_keep_the_stored_values(
        self, admin_client: Client, person: RevelUser, group: Group
    ) -> None:
        data = _browser_post_data(admin_client, person, bio="Edited")
        RevelUser.objects.filter(pk=person.pk).update(totp_active=True, preferred_name="Countess")
        person.groups.remove(group)

        response = admin_client.post(reverse("admin:accounts_reveluser_change", args=[person.pk]), data)

        assert response.status_code == 302
        changed = RevelUser.objects.get(pk=person.pk)
        assert (changed.bio, changed.totp_active, changed.preferred_name) == ("Edited", True, "Countess")
        assert not changed.groups.exists()

    def test_a_relation_that_the_admin_changes_is_saved(
        self, admin_client: Client, person: RevelUser, group: Group
    ) -> None:
        other = Group.objects.create(name="Door")
        data = _browser_post_data(admin_client, person, groups=[str(group.pk), str(other.pk)])

        response = admin_client.post(reverse("admin:accounts_reveluser_change", args=[person.pk]), data)

        assert response.status_code == 302
        assert set(person.groups.values_list("name", flat=True)) == {"Helpers", "Door"}

    def test_a_field_that_the_admin_changes_is_saved_also_when_the_row_changed_too(
        self, admin_client: Client, person: RevelUser
    ) -> None:
        data = _browser_post_data(admin_client, person, preferred_name="Ada L.")
        RevelUser.objects.filter(pk=person.pk).update(preferred_name="Countess")

        response = admin_client.post(reverse("admin:accounts_reveluser_change", args=[person.pk]), data)

        assert response.status_code == 302
        assert RevelUser.objects.get(pk=person.pk).preferred_name == "Ada L."

    def test_a_relation_that_the_page_showed_empty_keeps_a_value_added_later(
        self, admin_client: Client, person: RevelUser
    ) -> None:
        """An empty many-to-many field renders no hidden input; the page still showed it as empty."""
        data = _browser_post_data(admin_client, person, bio="Edited")
        assert "initial-groups" not in data
        person.groups.add(Group.objects.create(name="Added later"))

        response = admin_client.post(reverse("admin:accounts_reveluser_change", args=[person.pk]), data)

        assert response.status_code == 302
        assert list(person.groups.values_list("name", flat=True)) == ["Added later"]
