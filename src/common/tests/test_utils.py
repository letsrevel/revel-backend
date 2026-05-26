import sys
import typing as t
from io import BytesIO
from unittest import mock

import piexif
import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.db.models import Q
from PIL import Image

from common.models import Tag
from common.utils import get_or_create_with_race_protection, strip_exif


@pytest.fixture
def image_with_exif() -> InMemoryUploadedFile:
    # 1. Create a base image
    image = Image.new("RGB", (10, 10), color="red")

    # 2. Inject EXIF using piexif
    exif_dict = {
        "0th": {
            piexif.ImageIFD.Orientation: 1,
            piexif.ImageIFD.ImageDescription: "Hello world".encode("utf-8"),
        },
        "Exif": {},
        "GPS": {},
        "1st": {},
        "thumbnail": None,
    }

    exif_bytes = piexif.dump(exif_dict)

    # 3. Save image to buffer with EXIF
    buffer = BytesIO()
    image.save(buffer, format="jpeg", exif=exif_bytes)
    buffer.seek(0)

    return InMemoryUploadedFile(
        file=buffer,
        field_name="test",
        name="test.jpg",
        content_type="image/jpeg",
        size=sys.getsizeof(buffer),
        charset=None,
    )


def test_strip_exif_removes_data(image_with_exif: InMemoryUploadedFile) -> None:
    # Confirm EXIF is there
    image = Image.open(image_with_exif)
    exif = image.getexif()
    assert dict(exif), "Image should contain EXIF before stripping"

    # Strip it
    stripped = strip_exif(image_with_exif)
    stripped.seek(0)

    # Confirm EXIF is gone
    image_stripped = Image.open(stripped)
    exif = image_stripped.getexif()
    assert not dict(exif), "EXIF should be stripped from image"


class TestGetOrCreateWithRaceProtection:
    """Tests for get_or_create_with_race_protection, focusing on the race paths.

    TimeStampedModel.save() runs full_clean(), so a UniqueConstraint violation can
    surface as ValidationError (from validate_constraints) when a racing row was
    already committed before our insert — not only as IntegrityError at INSERT.
    The helper must treat that as a lost race and re-fetch the winner.
    """

    @pytest.mark.django_db
    def test_creates_when_absent(self) -> None:
        """Happy path: creates the row and reports created=True."""
        tag, created = get_or_create_with_race_protection(Tag, Q(name="race-tag-a"), {"name": "race-tag-a"})

        assert created is True
        assert tag.name == "race-tag-a"

    @pytest.mark.django_db
    def test_returns_existing_when_present(self) -> None:
        """Happy path: returns the existing row with created=False (no insert attempt)."""
        existing = Tag.objects.create(name="race-tag-b")

        tag, created = get_or_create_with_race_protection(Tag, Q(name="race-tag-b"), {"name": "race-tag-b"})

        assert created is False
        assert tag.pk == existing.pk

    @pytest.mark.django_db
    def test_recovers_from_validationerror_race(self) -> None:
        """A ValidationError from a racing duplicate is caught and the winner re-fetched."""

        def racing_create(**defaults: t.Any) -> None:
            # Simulate the race: a concurrent request commits the row (bulk_create
            # bypasses full_clean), then our full_clean's validate_constraints fails.
            Tag.objects.bulk_create([Tag(**defaults)])
            raise ValidationError("Tag with this Name already exists.")

        with mock.patch.object(Tag.objects, "create", side_effect=racing_create):
            tag, created = get_or_create_with_race_protection(Tag, Q(name="race-tag-c"), {"name": "race-tag-c"})

        assert created is False
        assert tag.name == "race-tag-c"

    @pytest.mark.django_db
    def test_reraises_non_race_validationerror(self) -> None:
        """A genuine (non-uniqueness) ValidationError still propagates when no row appears."""

        def failing_create(**defaults: t.Any) -> None:
            raise ValidationError("some other validation error")

        with mock.patch.object(Tag.objects, "create", side_effect=failing_create):
            with pytest.raises(ValidationError):
                get_or_create_with_race_protection(Tag, Q(name="race-tag-d"), {"name": "race-tag-d"})
