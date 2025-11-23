import secrets
import string
import typing as t

from django.conf import settings
from django.contrib.gis.db import models
from django.core.exceptions import ValidationError
from django.core.files.images import get_image_dimensions
from django.core.files.uploadedfile import UploadedFile
from django.core.validators import FileExtensionValidator
from django.utils.text import slugify

from common.models import TimeStampedModel
from geo.models import City


class VisibilityMixin(models.Model):
    class Visibility(models.TextChoices):
        PUBLIC = "public"  # everyone can see
        PRIVATE = "private"  # only invited people can see
        MEMBERS_ONLY = "members-only"  # only members can see
        STAFF_ONLY = "staff-only"  # only staff members can see

    visibility = models.CharField(choices=Visibility.choices, max_length=20, db_index=True, default=Visibility.PRIVATE)

    class Meta:
        abstract = True


class SlugFromNameMixin(models.Model):
    class Meta:
        abstract = True

    def save(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Override save to auto-create slug."""
        if not self.slug:  # type: ignore[has-type]
            self.slug = slugify(self.name)  # type: ignore[attr-defined]
        super().save(*args, **kwargs)


class LocationMixin(models.Model):
    city = models.ForeignKey(City, on_delete=models.SET_NULL, null=True, blank=True)
    location = models.PointField(geography=True, db_index=True, null=True, blank=True)
    address = models.CharField(blank=True, null=True, max_length=255)

    class Meta:
        abstract = True

    def save(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Override save to auto-create location."""
        if self.city and self.location is None:
            self.location = self.city.location
        super().save(*args, **kwargs)

    def full_address(self) -> str:
        """Get the full address combining address and city.

        Returns:
            Full address string, or empty string if no location info available.
        """
        if self.address and self.city:
            return f"{self.address}, {self.city}"
        if self.address:
            return self.address
        if self.city:
            return self.city.name
        return ""


ALLOWED_IMAGE_EXTENSIONS: list[str] = ["jpg", "jpeg", "png", "gif", "webp"]
MAX_IMAGE_SIZE_BYTES: int = 5 * 1024 * 1024  # 5MB


def validate_image_file(file: UploadedFile) -> None:
    """Validates an uploaded image."""
    if file.size > MAX_IMAGE_SIZE_BYTES:  # type: ignore[operator]
        raise ValidationError(f"Image must be under {MAX_IMAGE_SIZE_BYTES // (1024 * 1024)}MB.")
    try:
        get_image_dimensions(file)
    except Exception:
        raise ValidationError("File is not a valid image.")


class ExifStripMixin(models.Model):
    IMAGE_FIELDS: t.Iterable[str]

    def _strip_exif_from_image_fields(self) -> None:
        from common.utils import strip_exif

        for field_name in self.IMAGE_FIELDS:
            file = getattr(self, field_name, None)
            if file:
                setattr(self, field_name, strip_exif(file))

    def save(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Override save to auto-strip exif from image fields."""
        self._strip_exif_from_image_fields()
        super().save(*args, **kwargs)

    class Meta:
        abstract = True


class LogoCoverValidationMixin(ExifStripMixin):
    IMAGE_FIELDS = (
        "logo",
        "cover_art",
    )

    class Meta:
        abstract = True

    image_validators: list[t.Callable[[UploadedFile], None]] = [
        FileExtensionValidator(allowed_extensions=ALLOWED_IMAGE_EXTENSIONS),
        validate_image_file,
    ]

    logo = models.ImageField(
        upload_to="logos",
        null=True,
        blank=True,
        validators=image_validators,
    )
    cover_art = models.ImageField(
        upload_to="cover-art",
        null=True,
        blank=True,
        validators=image_validators,
    )


CODE_ALPHABET = string.ascii_letters + string.digits  # [a-zA-Z0-9]
CODE_LENGTH = 8


def secure_random_code() -> str:
    """Generate a secure random alphanumeric code of length CODE_LENGTH."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


class TokenMixin(TimeStampedModel):
    id = models.CharField(primary_key=True, max_length=32, editable=False, default=secure_random_code)  # type: ignore[assignment]
    name = models.CharField(max_length=120, null=True, blank=True)
    issuer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="%(class)s_tokens")
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    uses = models.IntegerField(default=0)
    max_uses = models.IntegerField(
        default=0, help_text="The maximum number of invites allowed for this token. 0 Means unlimited."
    )

    class Meta:
        abstract = True


class UserRequestMixin(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending"
        APPROVED = "approved"
        REJECTED = "rejected"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    message = models.TextField(null=True, blank=True, db_index=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="%(class)s_decided_by"
    )

    class Meta:
        abstract = True
