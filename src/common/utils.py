import functools
import mimetypes
import typing as t
from io import BytesIO

from django.core.exceptions import ValidationError
from django.core.files import File
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.db import IntegrityError, models, transaction
from django.db.models.fields.files import FieldFile
from django.http import HttpResponse
from PIL import Image
from pydantic import BaseModel

from common.fields import MAX_IMAGE_PIXELS

# ``Image.info`` keys that carry metadata (EXIF incl. GPS, XMP, JPEG comments). Pillow's
# built-in encoders only write what is passed to ``save()``, but plugins such as
# pillow-heif copy ``info`` wholesale into the output, so the keys must be dropped from
# the source image itself rather than merely left out of the ``save()`` call.
_METADATA_INFO_KEYS = ("exif", "xmp", "XML:com.adobe.xmp", "Raw profile type exif", "comment")


def strip_exif(image_file: File) -> InMemoryUploadedFile:  # type: ignore[type-arg]
    """Strip EXIF data from a Django File or InMemoryUploadedFile.

    Raises:
        ValidationError: If the image exceeds ``MAX_IMAGE_PIXELS``. Checked from the
            header before any pixel is decoded, so every image path is bounded here —
            including ones (questionnaire uploads) that skip ``validate_image_file``.
    """
    try:
        image = Image.open(image_file)
    except Image.DecompressionBombError:
        raise ValidationError(f"Image must be under {MAX_IMAGE_PIXELS // 1_000_000} megapixels.")
    # Image.open is lazy: width/height come from the header, so this bounds the raster
    # before the save below decodes it (memory-DoS guard).
    if image.width * image.height > MAX_IMAGE_PIXELS:
        raise ValidationError(f"Image must be under {MAX_IMAGE_PIXELS // 1_000_000} megapixels.")
    _format = image.format or "JPEG"

    output = BytesIO()
    # Drop the metadata from ``info`` and re-save the same image object. This strips
    # EXIF for every registered encoder without materializing the decoded raster an
    # extra ~3x (Image.frombytes on tobytes()) — a memory-DoS guard for large images.
    for key in _METADATA_INFO_KEYS:
        image.info.pop(key, None)
    image.save(output, format=_format)
    output.seek(0)

    # Try to infer some optional fields
    field_name = getattr(image_file, "field_name", "image")
    name = getattr(image_file, "name", "image.jpg")
    content_type = getattr(image_file, "content_type", "image/jpeg")

    return InMemoryUploadedFile(
        output,
        field_name=field_name,
        name=name,
        content_type=content_type,
        size=output.getbuffer().nbytes,
        charset=None,
    )


def assert_image_equal(actual_bytes: bytes, expected_bytes: bytes) -> None:
    """Assert that two images are visually identical by comparing pixel data.

    Args:
        actual_bytes: The saved image bytes (e.g. from .read())
        expected_bytes: The original image bytes (e.g. uploaded or fixture)
    """
    img1 = Image.open(BytesIO(actual_bytes)).convert("RGB")
    img2 = Image.open(BytesIO(expected_bytes)).convert("RGB")

    assert img1.size == img2.size, f"Image size mismatch: {img1.size} vs {img2.size}"

    assert img1.tobytes() == img2.tobytes(), "Image pixel data mismatch"


T = t.TypeVar("T", bound=models.Model)


def get_or_create_with_race_protection(
    model: type[T],
    lookup_filter: models.Q,
    defaults: dict[str, t.Any],
) -> tuple[T, bool]:
    """Get or create a model instance with protection against race conditions.

    Attempts to retrieve an instance matching the lookup filter. If not found,
    creates one using the defaults. A concurrent create that wins the race is
    handled by retrying the lookup. The race surfaces as either ``IntegrityError``
    (unique violation at INSERT) or ``ValidationError`` (``TimeStampedModel.save``
    runs ``full_clean``, so ``validate_constraints`` raises when the racing row was
    already committed before our insert); both are caught. A ``ValidationError``
    that is *not* a uniqueness race (no matching row appears) is re-raised.

    Args:
        model: The Django model class
        lookup_filter: Q object for filtering the lookup
        defaults: Dictionary of field values for creating the instance

    Returns:
        Tuple of (instance, created) where created is True if the instance was created

    Example:
        food_item, created = get_or_create_with_race_protection(
            FoodItem,
            Q(name__iexact="peanuts"),
            {"name": "Peanuts"}
        )
    """
    manager: models.Manager[T] = getattr(model, "objects")
    instance = manager.filter(lookup_filter).first()
    if instance:
        return instance, False

    try:
        with transaction.atomic():
            return manager.create(**defaults), True
    except IntegrityError, ValidationError:
        # Race condition: another request created the row between our check and
        # create. Depending on timing this raises IntegrityError (INSERT) or
        # ValidationError (full_clean's validate_constraints, when the racing row
        # is already committed). Re-fetch to return the winner.
        instance = manager.filter(lookup_filter).first()
        if not instance:
            # Not a uniqueness race (e.g. a genuine validation error): re-raise.
            raise
        return instance, False


def update_or_create_with_race_protection(
    model: type[T],
    lookup: dict[str, t.Any],
    defaults: dict[str, t.Any],
) -> tuple[T, bool]:
    """``update_or_create`` that survives a lost creation race.

    Django's own ``update_or_create`` recovers from an ``IntegrityError`` at INSERT,
    but **not** from the ``ValidationError`` that ``TimeStampedModel.save``'s
    ``full_clean`` raises (``validate_constraints``) when a racing row was already
    committed before our insert. This wrapper catches both, re-fetches the winner,
    applies ``defaults`` to it and saves — so the caller always gets the row in the
    state it asked for. A ``ValidationError`` that is *not* a uniqueness race (no
    matching row appears) is re-raised, and re-applying ``defaults`` to the winner
    re-raises a genuine model-level validation error rather than hiding it.

    Unlike :func:`get_or_create_with_race_protection` the lookup is a kwargs dict,
    not a ``Q``: the same keys identify the row *and* seed the created one.

    Args:
        model: The Django model class
        lookup: Field lookups identifying the row (also used as create kwargs)
        defaults: Field values to set on both the update and the create path

    Returns:
        Tuple of (instance, created) where created is True if the instance was created
    """
    manager: models.Manager[T] = getattr(model, "objects")
    try:
        with transaction.atomic():
            return manager.update_or_create(**lookup, defaults=defaults)
    except IntegrityError, ValidationError:
        instance = manager.filter(**lookup).first()
        if not instance:
            # Not a uniqueness race (e.g. a genuine validation error): re-raise.
            raise
        for field, value in defaults.items():
            setattr(instance, field, value)
        instance.save()
        return instance, False


@transaction.atomic
def update_db_instance(
    instance: T,
    payload: BaseModel | None = None,
    *,
    exclude_unset: bool = True,
    exclude_defaults: bool = False,
    exclude: set[str] | None = None,
    **kwargs: t.Any,
) -> T:
    """Updates a DB instance given a Pydantic payload, safely within a select_for_update lock."""
    instance = instance.__class__.objects.select_for_update().get(pk=instance.pk)  # type: ignore[attr-defined]
    data = (
        payload.model_dump(exclude_unset=exclude_unset, exclude_defaults=exclude_defaults, exclude=exclude)
        if payload
        else {}
    )
    data.update(**kwargs)
    for key, value in data.items():
        setattr(instance, key, value)
    instance.save()
    return instance


@functools.lru_cache(maxsize=1)
def _placeholder_png_bytes() -> bytes:
    """A neutral 150x150 solid-gray PNG, generated once per process."""
    buffer = BytesIO()
    Image.new("RGB", (150, 150), (203, 213, 225)).save(buffer, format="PNG")
    return buffer.getvalue()


def serve_image_or_placeholder(*file_fields: FieldFile | None) -> HttpResponse:
    """Serve the first readable image field's bytes, or a neutral placeholder.

    Backs stable image URLs embedded in external artifacts (e.g. Google
    Wallet save links in old emails): uploads delete the replaced file from
    storage, so these URLs must degrade — never error — when a file was
    replaced, removed, or never set. Candidates are tried in order (e.g.
    thumbnail, then original) so a broken optimized variant falls back to
    the original rather than the placeholder (same contract as
    ``wallet.apple.images.resolve_cover_art``, #358).

    Args:
        file_fields: Django FileField/ImageField values, best first; falsy
            entries are skipped.

    Returns:
        A cacheable image response.
    """
    body = _placeholder_png_bytes()
    content_type = "image/png"
    for file_field in file_fields:
        if not file_field:
            continue
        try:
            with file_field.open("rb") as f:
                body = f.read()
            content_type = mimetypes.guess_type(file_field.name or "")[0] or "application/octet-stream"
            break
        except OSError, ValueError:
            continue
    response = HttpResponse(body, content_type=content_type)
    response["Cache-Control"] = "public, max-age=3600"
    return response
