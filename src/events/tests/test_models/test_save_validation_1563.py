"""A save validates what it writes, and a stored image is not read again (#1563).

``TimeStampedModel.save`` runs ``full_clean``. With ``update_fields`` it now checks only the changed fields
with ``clean_fields``; ``clean()``, the unique checks and the constraints still run on the whole row. A file
that is already in storage was validated at its upload, so ``validate_image_file`` no longer reads it again:
before, a missing file failed every save of its row with ``FileNotFoundError``.
"""

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from events.models import Event
from events.tasks import build_attendee_visibility_flags

pytestmark = pytest.mark.django_db

MISSING = "events/missing-logo.png"


def _fresh(event: Event) -> Event:
    return Event.objects.get(pk=event.pk)


class TestPartialSave:
    def test_a_field_that_it_does_not_change_is_not_validated(self, event: Event) -> None:
        Event.objects.filter(pk=event.pk).update(slug="not a slug!")
        stored = _fresh(event)
        stored.attendee_count = 3

        stored.save(update_fields=["attendee_count"])

        assert _fresh(event).attendee_count == 3

    def test_a_field_that_it_changes_is_still_validated(self, event: Event) -> None:
        stored = _fresh(event)
        stored.slug = "not a slug!"

        with pytest.raises(ValidationError) as error:
            stored.save(update_fields=["slug"])

        assert "slug" in error.value.message_dict

    def test_the_cross_field_rules_still_run(self, event: Event) -> None:
        """``clean()`` checks the whole row: fix the data first (the error names the field)."""
        Event.objects.filter(pk=event.pk).update(end=event.start - timedelta(hours=1))
        stored = _fresh(event)
        stored.attendee_count = 3

        with pytest.raises(ValidationError) as error:
            stored.save(update_fields=["attendee_count"])

        assert "end" in error.value.message_dict

    def test_a_full_save_still_validates_every_field(self, event: Event) -> None:
        Event.objects.filter(pk=event.pk).update(slug="not a slug!")

        with pytest.raises(ValidationError) as error:
            _fresh(event).save()

        assert "slug" in error.value.message_dict


class TestStoredImage:
    def test_a_stored_image_that_is_missing_does_not_fail_a_save(self, event: Event) -> None:
        Event.objects.filter(pk=event.pk).update(logo=MISSING)
        stored = _fresh(event)
        stored.name = "Renamed"

        stored.save()

        assert _fresh(event).name == "Renamed"

    def test_a_new_upload_is_still_validated(self, event: Event) -> None:
        stored = _fresh(event)
        stored.logo = SimpleUploadedFile("logo.png", b"not an image", content_type="image/png")

        with pytest.raises(ValidationError) as error:
            stored.save()

        assert "logo" in error.value.message_dict

    def test_the_attendee_rebuild_runs_for_an_event_with_a_missing_image(self, event: Event) -> None:
        """The production failure: the rebuild task saved ``attendee_count`` and read the missing logo."""
        Event.objects.filter(pk=event.pk).update(logo=MISSING, cover_art=MISSING, attendee_count=7)

        build_attendee_visibility_flags(str(event.pk))

        assert _fresh(event).attendee_count == 0
