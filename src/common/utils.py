import hashlib
import sys
import typing as t
from io import BytesIO

from django.contrib.auth.models import AbstractUser
from django.core.files import File
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.db import IntegrityError, models, transaction
from PIL import Image

from common import tasks

from .models import FileUploadAudit


def strip_exif(image_file: File) -> InMemoryUploadedFile:  # type: ignore[type-arg]
    """Strip EXIF data from a Django File or InMemoryUploadedFile."""
    image = Image.open(image_file)
    data = list(image.getdata())
    image_no_exif = Image.new(image.mode, image.size)
    image_no_exif.putdata(data)

    output = BytesIO()
    _format = image.format or "JPEG"
    image_no_exif.save(output, format=_format)
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
        size=sys.getsizeof(output),
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

    pixels1 = list(img1.getdata())
    pixels2 = list(img2.getdata())

    assert pixels1 == pixels2, "Image pixel data mismatch"


T = t.TypeVar("T", bound=models.Model)


def get_or_create_with_race_protection(
    model: type[T],
    lookup_filter: models.Q,
    defaults: dict[str, t.Any],
) -> tuple[T, bool]:
    """Get or create a model instance with protection against race conditions.

    Attempts to retrieve an instance matching the lookup filter. If not found,
    creates one using the defaults. Handles IntegrityError from race conditions
    by retrying the lookup.

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
        return manager.create(**defaults), True
    except IntegrityError:
        # Race condition: another request created it between our check and create
        instance = manager.filter(lookup_filter).first()
        if not instance:
            # Should never happen, but if it does, re-raise the original error
            raise
        return instance, False


@transaction.atomic
def safe_save_uploaded_file(
    *,
    instance: T,
    field: str,
    file: File,  # type: ignore[type-arg]
    uploader: AbstractUser,
) -> T:
    """Safely save an uploaded file passing it to malware scan.

    Deletes the old file if one exists before saving the new file.
    """
    app = instance._meta.app_label
    model = t.cast(str, instance._meta.model_name)

    # Delete old file if it exists
    old_file = getattr(instance, field)
    if old_file:
        old_file.delete(save=False)

    setattr(instance, field, file)
    instance.save()
    file_field = getattr(instance, field)
    file_field.open()
    # minor risk of race condition if uploaded twice in rapid succession
    file_hash = hashlib.sha256(file_field.read()).hexdigest()
    file_field.seek(0)
    FileUploadAudit.objects.create(
        app=app, model=model, instance_pk=instance.pk, field=field, file_hash=file_hash, uploader=uploader.email
    )
    tasks.scan_for_malware.delay(app=app, model=model, pk=str(instance.pk), field=field)
    return instance
