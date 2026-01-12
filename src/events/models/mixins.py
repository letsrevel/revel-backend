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


class ResourceVisibility(models.TextChoices):
    """Visibility enum for resources with attendee-only option.

    Includes all base visibility options plus ATTENDEES_ONLY.
    """

    PUBLIC = "public"  # everyone can see
    PRIVATE = "private"  # only invited people can see
    MEMBERS_ONLY = "members-only"  # only members can see
    STAFF_ONLY = "staff-only"  # only staff members can see
    ATTENDEES_ONLY = "attendees-only"  # only users with tickets or RSVPs can see


class VisibilityMixin(models.Model):
    class Visibility(models.TextChoices):
        """Base visibility enum for events and resources."""

        PUBLIC = "public"  # everyone can see
        PRIVATE = "private"  # only invited people can see
        MEMBERS_ONLY = "members-only"  # only members can see
        STAFF_ONLY = "staff-only"  # only staff members can see

    visibility = models.CharField(choices=Visibility.choices, max_length=20, db_index=True, default=Visibility.PRIVATE)

    class Meta:
        abstract = True


SLUG_SUFFIX_ALPHABET = string.ascii_lowercase + string.digits  # [a-z0-9]
SLUG_SUFFIX_LENGTH = 5
MAX_SLUG_COLLISION_RETRIES = 10


def generate_slug_suffix() -> str:
    """Generate a short random suffix for slug collision resolution."""
    return "".join(secrets.choice(SLUG_SUFFIX_ALPHABET) for _ in range(SLUG_SUFFIX_LENGTH))


class SlugFromNameMixin(models.Model):
    """Mixin that auto-generates a slug from the name field.

    Handles slug collisions by appending a random suffix when needed.
    Subclasses can define `slug_scope_field` to specify a field that
    defines the uniqueness scope (e.g., 'organization' for Event).
    """

    # Override in subclass to specify the field that scopes slug uniqueness
    # e.g., slug_scope_field = "organization" means slug must be unique per organization
    slug_scope_field: str | None = None

    class Meta:
        abstract = True

    def _get_slug_queryset(self) -> models.QuerySet[t.Any]:
        """Get queryset for checking slug uniqueness within scope."""
        qs = self.__class__.objects.all()  # type: ignore[attr-defined]

        # Exclude self if already saved
        if self.pk:
            qs = qs.exclude(pk=self.pk)

        # Apply scope filter if defined
        if self.slug_scope_field:
            scope_value = getattr(self, self.slug_scope_field, None)
            if scope_value is not None:
                # Handle both FK and FK_id patterns
                field_name = self.slug_scope_field
                if hasattr(scope_value, "pk"):
                    field_name = f"{self.slug_scope_field}_id"
                    scope_value = scope_value.pk
                qs = qs.filter(**{field_name: scope_value})

        return qs  # type: ignore[no-any-return]

    def _generate_unique_slug(self, base_slug: str) -> str:
        """Generate a unique slug, appending a suffix if necessary."""
        qs = self._get_slug_queryset()

        # Try the base slug first
        if not qs.filter(slug=base_slug).exists():
            return base_slug

        # Collision detected - append random suffix
        for _ in range(MAX_SLUG_COLLISION_RETRIES):
            candidate = f"{base_slug}-{generate_slug_suffix()}"
            if not qs.filter(slug=candidate).exists():
                return candidate

        # Extremely unlikely - all retries collided
        raise ValueError(f"Could not generate unique slug after {MAX_SLUG_COLLISION_RETRIES} attempts")

    def save(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Override save to auto-create slug."""
        if not self.slug:  # type: ignore[has-type]
            base_slug = slugify(self.name)  # type: ignore[attr-defined]
            self.slug = self._generate_unique_slug(base_slug)
        super().save(*args, **kwargs)


class LocationMixin(models.Model):
    city = models.ForeignKey(City, on_delete=models.SET_NULL, null=True, blank=True)
    location = models.PointField(geography=True, db_index=True, null=True, blank=True)
    address = models.CharField(blank=True, null=True, max_length=255)
    location_maps_url = models.URLField(
        blank=True,
        null=True,
        help_text="Shareable link to Google Maps (e.g., https://goo.gl/maps/...)",
    )
    location_maps_embed = models.URLField(
        blank=True,
        null=True,
        max_length=2048,
        help_text="Embed URL for iframe src (e.g., https://www.google.com/maps/embed?pb=...)",
    )

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


class SocialMediaMixin(models.Model):
    instagram_url = models.URLField("Instagram", blank=True, null=True)
    facebook_url = models.URLField("Facebook", blank=True, null=True)
    bluesky_url = models.URLField("Bluesky", blank=True, null=True)
    telegram_url = models.URLField("Telegram", blank=True, null=True)

    class Meta:
        abstract = True
