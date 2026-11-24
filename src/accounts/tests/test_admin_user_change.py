"""The admin change form and the admin password change write only what the admin changed."""

import typing as t
from html.parser import HTMLParser
from unittest.mock import patch

import pytest
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import Group
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models.signals import post_save
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

    def test_the_save_signals_see_the_stored_values_of_the_fields_that_the_admin_did_not_change(
        self, admin_client: Client, person: RevelUser
    ) -> None:
        data = _browser_post_data(admin_client, person, bio="Edited")
        RevelUser.objects.filter(pk=person.pk).update(email="new@example.com", email_verified=True)
        seen: list[tuple[str, bool, str]] = []

        def receiver(instance: RevelUser, **kwargs: t.Any) -> None:
            seen.append((instance.email, instance.email_verified, instance.bio))

        post_save.connect(receiver, sender=RevelUser, dispatch_uid="test-admin-saved-instance")
        try:
            response = admin_client.post(reverse("admin:accounts_reveluser_change", args=[person.pk]), data)
        finally:
            post_save.disconnect(sender=RevelUser, dispatch_uid="test-admin-saved-instance")

        assert response.status_code == 302
        assert seen == [("new@example.com", True, "Edited")]

    def test_a_relation_added_on_a_page_that_had_a_validation_error_is_saved_after_the_correction(
        self, admin_client: Client, person: RevelUser, django_user_model: type[RevelUser]
    ) -> None:
        """The page showed no groups; the first post fails, the corrected post must still add the group."""
        taken = django_user_model.objects.create_user(username="taken@example.com", email="taken@example.com")
        group = Group.objects.create(name="Added on the page")
        url = reverse("admin:accounts_reveluser_change", args=[person.pk])
        data = _browser_post_data(admin_client, person, groups=[str(group.pk)], username=taken.username)

        failed = admin_client.post(url, data)
        assert failed.status_code == 200  # the form again, with the error
        parser = _ShownValues()
        parser.feed(failed.content.decode())
        response = admin_client.post(url, data | parser.values | {"username": person.username})

        assert response.status_code == 302
        assert list(person.groups.values_list("name", flat=True)) == ["Added on the page"]


class TestProfilePicture:
    """A new or removed picture in the admin replaces the derived images, as an upload through the API does."""

    FILES = ("profile_picture", "profile_picture_thumbnail", "profile_picture_preview")

    @pytest.fixture
    def old_files(self, person: RevelUser, settings: t.Any, tmp_path: t.Any) -> list[str]:
        settings.MEDIA_ROOT = str(tmp_path)
        storage = RevelUser._meta.get_field("profile_picture").storage
        names = {
            field: storage.save(f"protected/profile-pictures/{person.pk}/{field}.png", ContentFile(b"image"))
            for field in self.FILES
        }
        RevelUser.objects.filter(pk=person.pk).update(**names)
        return list(names.values())

    def _post(self, client: Client, person: RevelUser, capture: t.Any, **overrides: t.Any) -> t.Any:
        url = reverse("admin:accounts_reveluser_change", args=[person.pk])
        data = _browser_post_data(client, person)
        data.pop("profile_picture", None)  # a browser sends a file only when the admin picks one
        data.update(overrides)
        with (
            patch("common.thumbnails.tasks.delete_orphaned_thumbnails_task") as delete,
            patch("common.thumbnails.tasks.generate_thumbnails_task") as generate,
            capture(execute=True),
        ):
            response = client.post(url, data)
            assert response.status_code == 302, response.context["adminform"].form.errors
        return delete, generate

    def test_a_new_picture_clears_the_derived_images_and_replaces_the_old_files_after_commit(
        self,
        admin_client: Client,
        person: RevelUser,
        old_files: list[str],
        rgb_image_bytes: bytes,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        upload = SimpleUploadedFile("new.jpg", rgb_image_bytes, content_type="image/jpeg")

        delete, generate = self._post(admin_client, person, django_capture_on_commit_callbacks, profile_picture=upload)

        picture, thumbnail, preview = RevelUser.objects.values_list(*self.FILES).get(pk=person.pk)
        assert picture.endswith(".jpg") and picture not in old_files
        assert not thumbnail and not preview
        delete.delay.assert_called_once_with(thumbnail_paths=old_files)
        generate.delay.assert_called_once_with(
            app="accounts", model="reveluser", pk=str(person.pk), field="profile_picture"
        )

    def test_a_removed_picture_clears_all_fields_and_deletes_the_files_after_commit(
        self, admin_client: Client, person: RevelUser, old_files: list[str], django_capture_on_commit_callbacks: t.Any
    ) -> None:
        delete, generate = self._post(
            admin_client, person, django_capture_on_commit_callbacks, **{"profile_picture-clear": "on"}
        )

        assert not any(RevelUser.objects.values_list(*self.FILES).get(pk=person.pk))
        delete.delay.assert_called_once_with(thumbnail_paths=old_files)
        generate.delay.assert_not_called()

    def test_an_edit_that_keeps_the_picture_keeps_the_files(
        self, admin_client: Client, person: RevelUser, old_files: list[str], django_capture_on_commit_callbacks: t.Any
    ) -> None:
        delete, generate = self._post(admin_client, person, django_capture_on_commit_callbacks, bio="Edited")

        assert list(RevelUser.objects.values_list(*self.FILES).get(pk=person.pk)) == old_files
        delete.delay.assert_not_called()
        generate.delay.assert_not_called()

    def test_a_first_picture_gets_derived_images_and_deletes_nothing(
        self,
        admin_client: Client,
        person: RevelUser,
        settings: t.Any,
        tmp_path: t.Any,
        rgb_image_bytes: bytes,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        settings.MEDIA_ROOT = str(tmp_path)
        upload = SimpleUploadedFile("first.jpg", rgb_image_bytes, content_type="image/jpeg")

        delete, generate = self._post(admin_client, person, django_capture_on_commit_callbacks, profile_picture=upload)

        assert RevelUser.objects.get(pk=person.pk).profile_picture
        delete.delay.assert_not_called()
        generate.delay.assert_called_once()

    def test_a_new_picture_with_the_same_stem_keeps_the_derived_paths_that_it_reuses(
        self,
        admin_client: Client,
        person: RevelUser,
        settings: t.Any,
        tmp_path: t.Any,
        rgb_image_bytes: bytes,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Derived paths come from the source stem: ``avatar.png`` and ``avatar.jpg`` share them."""
        from common.thumbnails.service import get_thumbnail_path

        settings.MEDIA_ROOT = str(tmp_path)
        folder = f"protected/profile-pictures/{person.pk}"
        old = default_storage.save(f"{folder}/avatar.png", ContentFile(b"image"))
        derived = [get_thumbnail_path(old, name) for name in self.FILES[1:]]
        RevelUser.objects.filter(pk=person.pk).update(
            profile_picture=old, profile_picture_thumbnail=derived[0], profile_picture_preview=derived[1]
        )
        upload = SimpleUploadedFile("avatar.jpg", rgb_image_bytes, content_type="image/jpeg")

        delete, _ = self._post(admin_client, person, django_capture_on_commit_callbacks, profile_picture=upload)

        delete.delay.assert_called_once_with(thumbnail_paths=[old])
