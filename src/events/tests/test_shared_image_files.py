"""A duplicated event shares the template's image files; a file goes only when no row names it."""

import typing as t
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.contrib import admin as django_admin
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile

from common.service.upload_service import safe_save_uploaded_file
from common.thumbnails.files import delete_unreferenced_files
from common.thumbnails.service import delete_image_with_derivatives
from events.admin.event import EventAdmin, EventSeriesAdmin
from events.models import Event, EventSeries, Organization
from events.service.duplication import duplicate_event

pytestmark = pytest.mark.django_db

IMAGE_FIELDS = ("logo", "logo_thumbnail", "cover_art", "cover_art_thumbnail", "cover_art_social")


@pytest.fixture
def template(event: Event, settings: t.Any, tmp_path: t.Any) -> Event:
    """An event with a stored file in each image field."""
    settings.MEDIA_ROOT = str(tmp_path)
    names = {
        name: default_storage.save(f"shared/{event.pk}/{name}.jpg", ContentFile(b"image")) for name in IMAGE_FIELDS
    }
    Event.objects.filter(pk=event.pk).update(**names)
    event.refresh_from_db()
    return event


def _paths(event: Event) -> list[str]:
    return [name for name in Event.objects.values_list(*IMAGE_FIELDS).get(pk=event.pk) if name]


def test_a_duplicate_shares_the_images_and_their_derived_images(template: Event) -> None:
    copy = duplicate_event(template, "Copy", template.start + timedelta(days=7))

    assert _paths(copy) == _paths(template)


def test_removing_the_logo_of_a_copy_keeps_the_shared_files(
    template: Event, django_capture_on_commit_callbacks: t.Any
) -> None:
    copy = duplicate_event(template, "Copy", template.start + timedelta(days=7))

    with django_capture_on_commit_callbacks(execute=True):
        delete_image_with_derivatives(copy, "logo")

    assert not Event.objects.get(pk=copy.pk).logo
    assert all(default_storage.exists(path) for path in _paths(template))


def test_removing_an_unshared_logo_deletes_its_files_after_commit(
    template: Event, django_capture_on_commit_callbacks: t.Any
) -> None:
    logo, thumbnail = str(template.logo.name), str(template.logo_thumbnail.name)
    with django_capture_on_commit_callbacks() as callbacks:
        delete_image_with_derivatives(template, "logo")
    assert default_storage.exists(logo)  # not before the commit

    for callback in callbacks:
        callback()

    assert not default_storage.exists(logo) and not default_storage.exists(thumbnail)


def test_a_new_upload_on_a_copy_keeps_the_shared_old_files(
    template: Event, rgb_image_bytes: bytes, django_capture_on_commit_callbacks: t.Any
) -> None:
    copy = duplicate_event(template, "Copy", template.start + timedelta(days=7))
    upload = SimpleUploadedFile("new.jpg", rgb_image_bytes, content_type="image/jpeg")

    with (
        patch("common.service.upload_service.create_file_audit_and_scan"),
        patch("common.service.upload_service.generate_thumbnails_task"),
        django_capture_on_commit_callbacks(execute=True),
    ):
        safe_save_uploaded_file(instance=copy, field="logo", file=upload, uploader=template.organization.owner)

    assert Event.objects.get(pk=copy.pk).logo.name != template.logo.name
    assert all(default_storage.exists(path) for path in _paths(template))


def test_a_new_upload_on_an_unshared_event_deletes_the_old_files_after_commit(
    template: Event, rgb_image_bytes: bytes, django_capture_on_commit_callbacks: t.Any
) -> None:
    old = [str(template.logo.name), str(template.logo_thumbnail.name)]
    upload = SimpleUploadedFile("new.jpg", rgb_image_bytes, content_type="image/jpeg")

    with (
        patch("common.service.upload_service.create_file_audit_and_scan"),
        patch("common.service.upload_service.generate_thumbnails_task"),
        django_capture_on_commit_callbacks() as callbacks,
    ):
        safe_save_uploaded_file(instance=template, field="logo", file=upload, uploader=template.organization.owner)
    assert all(default_storage.exists(path) for path in old)  # a rollback would still find them

    for callback in callbacks:
        callback()

    assert not any(default_storage.exists(path) for path in old)


def test_deleting_a_copy_keeps_the_shared_files(template: Event, django_capture_on_commit_callbacks: t.Any) -> None:
    copy = duplicate_event(template, "Copy", template.start + timedelta(days=7))

    with django_capture_on_commit_callbacks(execute=True):
        copy.delete()

    assert all(default_storage.exists(path) for path in _paths(template))


def test_deleting_the_last_event_that_names_the_files_deletes_them(
    template: Event, django_capture_on_commit_callbacks: t.Any
) -> None:
    paths = _paths(template)

    with django_capture_on_commit_callbacks(execute=True):
        template.delete()

    assert not any(default_storage.exists(path) for path in paths)


def test_a_source_path_that_another_row_names_is_kept(template: Event) -> None:
    """The check covers source fields too: a copy can name only the template's source file."""
    other = duplicate_event(template, "Copy", template.start + timedelta(days=7))
    Event.objects.filter(pk=other.pk).update(logo_thumbnail=None, cover_art_thumbnail=None, cover_art_social=None)
    Event.objects.filter(pk=template.pk).update(logo=None)  # only the copy names the file now

    logo = str(template.logo.name)
    delete_unreferenced_files([logo])

    assert default_storage.exists(logo)


def test_a_delete_reads_the_stored_paths_not_the_loaded_ones(
    template: Event, django_capture_on_commit_callbacks: t.Any
) -> None:
    """A thumbnail written after the event was loaded is deleted with the event."""
    loaded = Event.objects.get(pk=template.pk)
    newer = default_storage.save(f"shared/{template.pk}/newer_thumbnail.jpg", ContentFile(b"image"))
    Event.objects.filter(pk=template.pk).update(logo_thumbnail=newer)

    with django_capture_on_commit_callbacks(execute=True):
        loaded.delete()

    assert not default_storage.exists(newer)


def test_a_series_delete_deletes_the_files_of_its_events(
    template: Event, django_capture_on_commit_callbacks: t.Any
) -> None:
    """The events go by cascade, so their ``delete()`` does not run; the series collects their files."""
    paths = _paths(template)
    series = t.cast(EventSeries, template.event_series)

    with django_capture_on_commit_callbacks(execute=True):
        series.delete()

    assert not Event.objects.filter(pk=template.pk).exists()
    assert not any(default_storage.exists(path) for path in paths)


def test_an_organization_delete_deletes_the_files_of_its_events(
    template: Event, django_capture_on_commit_callbacks: t.Any
) -> None:
    paths = _paths(template)
    organization = Organization.objects.get(pk=template.organization_id)

    with django_capture_on_commit_callbacks(execute=True):
        organization.delete()

    assert not any(default_storage.exists(path) for path in paths)


def test_a_regeneration_for_a_copy_keeps_the_template_files(template: Event, rgb_image_bytes: bytes) -> None:
    """The derived path of a shared source is the same for every row; a regeneration must not delete it."""
    from common.thumbnails.service import get_thumbnail_path
    from common.thumbnails.tasks import generate_thumbnails_task

    source = default_storage.save(f"shared/{template.pk}/photo.jpg", ContentFile(rgb_image_bytes))
    derived = default_storage.save(get_thumbnail_path(source, "logo_thumbnail"), ContentFile(b"template thumb"))
    Event.objects.filter(pk=template.pk).update(logo=source, logo_thumbnail=derived)
    copy = duplicate_event(template, "Copy", template.start + timedelta(days=7))

    result = generate_thumbnails_task.run(app="events", model="event", pk=str(copy.pk), field="logo")

    assert result["logo_thumbnail"] != derived  # the copy got its own file
    assert default_storage.exists(derived)  # still named by the template
    with default_storage.open(derived, "rb") as file:
        assert file.read() == b"template thumb"  # not rewritten
    assert Event.objects.get(pk=copy.pk).logo_thumbnail.name == result["logo_thumbnail"]


class TestAdmin:
    """The Event and EventSeries change forms keep derived images in step, as an API upload does."""

    def test_a_new_logo_clears_the_derived_image_and_replaces_the_old_files_after_commit(
        self, template: Event, rgb_image_bytes: bytes, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        model_admin = EventAdmin(Event, django_admin.site)
        old = [template.logo.name, template.logo_thumbnail.name]
        template.logo = SimpleUploadedFile("new.jpg", rgb_image_bytes, content_type="image/jpeg")
        form = MagicMock(changed_data=["logo"], initial={})

        with (
            patch("common.thumbnails.tasks.delete_orphaned_thumbnails_task") as delete,
            patch("common.thumbnails.tasks.generate_thumbnails_task") as generate,
            django_capture_on_commit_callbacks(execute=True),
        ):
            model_admin.save_model(MagicMock(), template, form, change=True)

        stored = Event.objects.get(pk=template.pk)
        assert stored.logo.name not in old and not stored.logo_thumbnail
        assert stored.cover_art_thumbnail  # an image that the form did not change keeps its derived images
        delete.delay.assert_called_once_with(thumbnail_paths=old)
        generate.delay.assert_called_once_with(app="events", model="event", pk=str(template.pk), field="logo")

    def test_a_removed_cover_art_clears_all_its_fields(
        self, template: Event, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        model_admin = EventAdmin(Event, django_admin.site)
        old = [template.cover_art.name, template.cover_art_thumbnail.name, template.cover_art_social.name]
        template.cover_art = ""  # the "clear" checkbox
        form = MagicMock(changed_data=["cover_art"], initial={})

        with (
            patch("common.thumbnails.tasks.delete_orphaned_thumbnails_task") as delete,
            patch("common.thumbnails.tasks.generate_thumbnails_task") as generate,
            django_capture_on_commit_callbacks(execute=True),
        ):
            model_admin.save_model(MagicMock(), template, form, change=True)

        stored = Event.objects.get(pk=template.pk)
        assert not stored.cover_art and not stored.cover_art_thumbnail and not stored.cover_art_social
        delete.delay.assert_called_once_with(thumbnail_paths=old)
        generate.delay.assert_not_called()

    def test_the_series_form_does_not_edit_derived_images(self) -> None:
        readonly = EventSeriesAdmin(EventSeries, django_admin.site).get_readonly_fields(MagicMock())

        assert {"logo_thumbnail", "cover_art_thumbnail", "cover_art_social"} <= set(readonly)

    def test_a_form_change_keeps_the_stored_paths_of_the_images_it_did_not_change(
        self, template: Event, rgb_image_bytes: bytes, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        """The form object can be older than the row: a thumbnail written since then is not undone."""
        model_admin = EventAdmin(Event, django_admin.site)
        loaded = Event.objects.get(pk=template.pk)
        newer = default_storage.save(f"shared/{template.pk}/newer_social.jpg", ContentFile(b"image"))
        Event.objects.filter(pk=template.pk).update(cover_art_social=newer)
        loaded.logo = SimpleUploadedFile("new.jpg", rgb_image_bytes, content_type="image/jpeg")
        form = MagicMock(changed_data=["logo"], initial={})

        with (
            patch("common.thumbnails.tasks.delete_orphaned_thumbnails_task"),
            patch("common.thumbnails.tasks.generate_thumbnails_task"),
            django_capture_on_commit_callbacks(execute=True),
        ):
            model_admin.save_model(MagicMock(), loaded, form, change=True)

        assert Event.objects.get(pk=template.pk).cover_art_social.name == newer

    def test_a_series_form_with_a_new_logo_replaces_its_files(
        self, template: Event, rgb_image_bytes: bytes, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        series = t.cast(EventSeries, template.event_series)
        old = default_storage.save(f"series/{series.pk}/logo.jpg", ContentFile(b"image"))
        EventSeries.objects.filter(pk=series.pk).update(logo=old)
        series.refresh_from_db()
        series.logo = SimpleUploadedFile("new.jpg", rgb_image_bytes, content_type="image/jpeg")

        with (
            patch("common.thumbnails.tasks.delete_orphaned_thumbnails_task") as delete,
            patch("common.thumbnails.tasks.generate_thumbnails_task") as generate,
            django_capture_on_commit_callbacks(execute=True),
        ):
            EventSeriesAdmin(EventSeries, django_admin.site).save_model(
                MagicMock(), series, MagicMock(changed_data=["logo"], initial={}), change=True
            )

        delete.delay.assert_called_once_with(thumbnail_paths=[old])
        generate.delay.assert_called_once_with(app="events", model="eventseries", pk=str(series.pk), field="logo")

    def test_an_added_event_with_a_logo_gets_derived_images_and_deletes_nothing(
        self, template: Event, rgb_image_bytes: bytes, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        new = Event(organization=template.organization, name="New", start=template.start)
        new.logo = SimpleUploadedFile("new.jpg", rgb_image_bytes, content_type="image/jpeg")

        with (
            patch("common.thumbnails.tasks.delete_orphaned_thumbnails_task") as delete,
            patch("common.thumbnails.tasks.generate_thumbnails_task") as generate,
            django_capture_on_commit_callbacks(execute=True),
        ):
            EventAdmin(Event, django_admin.site).save_model(
                MagicMock(), new, MagicMock(changed_data=["logo"], initial={}), change=False
            )

        delete.delay.assert_not_called()
        generate.delay.assert_called_once_with(app="events", model="event", pk=str(new.pk), field="logo")
