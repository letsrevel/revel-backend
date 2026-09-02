import sys
import typing as t
from contextlib import contextmanager
from io import BytesIO
from unittest import mock

import piexif
import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.db import IntegrityError
from django.db.models import Q
from PIL import Image

from common.models import Tag
from common.utils import get_or_create_with_race_protection, strip_exif, update_or_create_with_race_protection


@contextmanager
def force_first_lookup_miss(manager: t.Any) -> t.Iterator[None]:
    """Make ``manager.filter(...).first()`` miss on its first call, then behave normally.

    Used to simulate a race where a concurrent winner has already committed before our
    create attempt, but our own pre-check ran too early to see it.
    """
    call_count = 0
    real_filter = manager.filter

    def fake_filter(*args: t.Any, **kwargs: t.Any) -> t.Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return manager.none()
        return real_filter(*args, **kwargs)

    with mock.patch.object(manager, "filter", side_effect=fake_filter):
        yield


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


@pytest.mark.django_db
def test_exif_strip_mixin_skips_committed_files(organization: t.Any, image_with_exif: InMemoryUploadedFile) -> None:
    """Saving a model with an already-stored image must not rewrite the file.

    ExifStripMixin only strips fresh (uncommitted) uploads; re-stripping a
    committed file would mark the field dirty and write a re-encoded duplicate
    to storage on every save().
    """
    organization.cover_art = image_with_exif
    organization.save()
    stored_name = organization.cover_art.name

    organization.refresh_from_db()
    organization.save()  # full save with a committed file
    organization.refresh_from_db()

    assert organization.cover_art.name == stored_name, "Committed image was rewritten on save"


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
        """A ValidationError from a racing duplicate is caught and the winner re-fetched.

        The winner is committed *before* our create attempt (mirroring a concurrent
        request's already-committed row), and our own pre-check is forced to miss it
        once so we still attempt the create, which fails with the ValidationError
        full_clean's validate_constraints raises when the racing row is already there.
        """
        existing = Tag.objects.create(name="race-tag-c")

        def failing_create(**defaults: t.Any) -> None:
            raise ValidationError("Tag with this Name already exists.")

        with (
            force_first_lookup_miss(Tag.objects),
            mock.patch.object(Tag.objects, "create", side_effect=failing_create),
        ):
            tag, created = get_or_create_with_race_protection(Tag, Q(name="race-tag-c"), {"name": "race-tag-c"})

        assert created is False
        assert tag.pk == existing.pk

    @pytest.mark.django_db
    def test_recovers_from_integrityerror_race_without_poisoning_transaction(self) -> None:
        """A genuine DB-level IntegrityError race is recovered via a savepoint.

        The "concurrent winner" row is committed to the ambient transaction *before*
        our own create attempt begins (mirroring a real race, where the winner commits
        in an independent transaction). Our own pre-check is forced to miss it (first
        call only) so we still attempt the create; full_clean is disabled for that
        attempt so it skips validate_unique and reaches the database for real, making
        the race surface as a raw IntegrityError from the unique index rather than a
        Django-level ValidationError. Without wrapping the create attempt in its own
        savepoint, that IntegrityError aborts the ambient (test) transaction, and the
        recovery re-fetch raises TransactionManagementError instead of returning the
        winner.
        """
        existing = Tag.objects.create(name="race-tag-e")

        with (
            force_first_lookup_miss(Tag.objects),
            mock.patch.object(Tag, "full_clean", return_value=None),
        ):
            tag, created = get_or_create_with_race_protection(Tag, Q(name="race-tag-e"), {"name": "race-tag-e"})

        assert created is False
        assert tag.pk == existing.pk

        # The ambient transaction must still be usable after the recovery.
        assert Tag.objects.filter(name="race-tag-e").count() == 1

    @pytest.mark.django_db
    def test_reraises_non_race_validationerror(self) -> None:
        """A genuine (non-uniqueness) ValidationError still propagates when no row appears."""

        def failing_create(**defaults: t.Any) -> None:
            raise ValidationError("some other validation error")

        with mock.patch.object(Tag.objects, "create", side_effect=failing_create):
            with pytest.raises(ValidationError):
                get_or_create_with_race_protection(Tag, Q(name="race-tag-d"), {"name": "race-tag-d"})


class TestUpdateOrCreateWithRaceProtection:
    """Tests for update_or_create_with_race_protection.

    Django's ``update_or_create`` already recovers from an ``IntegrityError`` at
    INSERT, but not from the ``ValidationError`` that ``TimeStampedModel.save``'s
    ``full_clean`` raises when a racing row committed first. The wrapper must
    absorb that, re-fetch the winner, and still apply ``defaults`` to it.
    """

    @pytest.mark.django_db
    def test_creates_when_absent(self) -> None:
        """Happy path: creates the row with lookup + defaults merged, created=True."""
        tag, created = update_or_create_with_race_protection(Tag, {"name": "uoc-tag-a"}, {"description": "fresh"})

        assert created is True
        assert tag.name == "uoc-tag-a"
        assert tag.description == "fresh"

    @pytest.mark.django_db
    def test_updates_when_present(self) -> None:
        """Happy path: updates the existing row in place with created=False."""
        existing = Tag.objects.create(name="uoc-tag-b", description="old")

        tag, created = update_or_create_with_race_protection(Tag, {"name": "uoc-tag-b"}, {"description": "new"})

        assert created is False
        assert tag.pk == existing.pk
        existing.refresh_from_db()
        assert existing.description == "new"

    @pytest.mark.django_db
    def test_recovers_from_validationerror_race_and_applies_defaults(self) -> None:
        """A lost creation race still leaves the winner carrying the requested defaults."""
        existing = Tag.objects.create(name="uoc-tag-c", description="old")

        with mock.patch.object(
            Tag.objects,
            "update_or_create",
            side_effect=ValidationError("Tag with this Name already exists."),
        ):
            tag, created = update_or_create_with_race_protection(Tag, {"name": "uoc-tag-c"}, {"description": "new"})

        assert created is False
        assert tag.pk == existing.pk
        existing.refresh_from_db()
        assert existing.description == "new"

    @pytest.mark.django_db
    def test_recovers_from_integrityerror_race(self) -> None:
        """The IntegrityError arm is covered too, for constraints Django's own retry misses."""
        existing = Tag.objects.create(name="uoc-tag-d", description="old")

        with mock.patch.object(
            Tag.objects,
            "update_or_create",
            side_effect=IntegrityError("duplicate key value violates unique constraint"),
        ):
            tag, created = update_or_create_with_race_protection(Tag, {"name": "uoc-tag-d"}, {"description": "new"})

        assert created is False
        assert tag.pk == existing.pk

    @pytest.mark.django_db
    def test_reraises_non_race_validationerror(self) -> None:
        """A genuine (non-uniqueness) ValidationError still propagates when no row appears."""
        with mock.patch.object(
            Tag.objects,
            "update_or_create",
            side_effect=ValidationError("some other validation error"),
        ):
            with pytest.raises(ValidationError):
                update_or_create_with_race_protection(Tag, {"name": "uoc-tag-e"}, {"description": "x"})


@pytest.fixture
def heif_with_exif() -> InMemoryUploadedFile:
    """A HEIF image (the iPhone default) carrying EXIF, including a GPS IFD."""
    exif_bytes = piexif.dump(
        {
            "0th": {piexif.ImageIFD.ImageDescription: b"Hello world"},
            "Exif": {},
            "GPS": {piexif.GPSIFD.GPSLatitude: ((48, 1), (12, 1), (0, 1))},
            "1st": {},
            "thumbnail": None,
        }
    )
    buffer = BytesIO()
    Image.new("RGB", (10, 10), color="red").save(buffer, format="HEIF", exif=exif_bytes)
    buffer.seek(0)
    return InMemoryUploadedFile(
        file=buffer,
        field_name="test",
        name="photo.heic",
        content_type="image/heic",
        size=buffer.getbuffer().nbytes,
        charset=None,
    )


def test_strip_exif_removes_data_from_heif(heif_with_exif: InMemoryUploadedFile) -> None:
    """pillow-heif's encoder copies ``Image.info`` (EXIF/XMP) into the output — unlike Pillow's
    built-in encoders — so leaving ``exif=`` out of ``save()`` alone would leak GPS for HEIC."""
    source = Image.open(heif_with_exif)
    assert dict(source.getexif().get_ifd(0x8825)), "fixture should carry a GPS IFD"
    heif_with_exif.seek(0)

    stripped = strip_exif(heif_with_exif)
    stripped.seek(0)

    image_stripped = Image.open(stripped)
    assert image_stripped.format == "HEIF"
    assert not dict(image_stripped.getexif()), "EXIF should be stripped from HEIF output"


def test_strip_exif_rejects_image_over_pixel_budget() -> None:
    """The pixel budget is enforced from the header, before decoding, for every strip_exif caller."""
    buffer = BytesIO()
    Image.new("L", (20, 20), color=255).save(buffer, format="PNG")
    buffer.seek(0)

    with mock.patch("common.utils.MAX_IMAGE_PIXELS", 100):
        with pytest.raises(ValidationError, match="megapixels"):
            strip_exif(buffer)  # type: ignore[arg-type]
