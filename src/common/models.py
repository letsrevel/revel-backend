import gzip
import typing as t
import uuid

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.db import models
from simple_history.models import HistoricalRecords
from solo.models import SingletonModel

from .fields import MarkdownField


class TimeStampedModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        abstract = True

    def save(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Override the save method to call full_clean before saving."""
        self.full_clean()
        super().save(*args, **kwargs)


class ExifStripMixin(models.Model):
    """Mixin that strips EXIF metadata from image fields on save.

    Subclasses must define IMAGE_FIELDS as an iterable of field names to process.
    """

    IMAGE_FIELDS: t.Iterable[str]

    def _strip_exif_from_image_fields(self) -> None:
        from django.core.exceptions import ValidationError
        from django.utils.translation import gettext_lazy as _
        from PIL import UnidentifiedImageError

        from common.utils import strip_exif

        for field_name in self.IMAGE_FIELDS:
            file = getattr(self, field_name, None)
            if file:
                try:
                    setattr(self, field_name, strip_exif(file))
                except (UnidentifiedImageError, OSError) as e:
                    # Convert PIL errors to ValidationError for proper 400 response.
                    # This is a safety net - validation should happen before save().
                    raise ValidationError({field_name: [_("File is not a valid image.")]}) from e

    def save(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Override save to auto-strip exif from image fields."""
        self._strip_exif_from_image_fields()
        super().save(*args, **kwargs)

    class Meta:
        abstract = True


class Legal(SingletonModel):
    """Singleton model for legal documents like Terms and Conditions and Privacy Policy."""

    terms_and_conditions = MarkdownField(
        help_text="Terms and Conditions text in markdown format", blank=True, default=""
    )
    privacy_policy = MarkdownField(help_text="Privacy Policy text in markdown format", blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)
    history = HistoricalRecords()

    def __str__(self) -> str:  # pragma: no cover
        return "Legal Documents"

    class Meta:
        verbose_name = "Legal Documents"


class SiteSettings(SingletonModel):
    """Singleton model for common application settings."""

    notify_user_joined = models.BooleanField(
        default=False, help_text="Send a notification when a new user joins the platform."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    data_retention_days = models.PositiveIntegerField(
        verbose_name="Data Retention Period",
        help_text="The number of days to retain data before deletion.",
        default=30,
    )
    live_emails = models.BooleanField(default=False, help_text="Live-emails enabled")
    frontend_base_url = models.URLField(default=settings.FRONTEND_BASE_URL)
    internal_catchall_email = models.EmailField(
        verbose_name="Internal Catchall Email",
        help_text="The catchall email address for internal use.",
        default=settings.INTERNAL_CATCHALL_EMAIL,
    )

    history = HistoricalRecords()

    def __str__(self) -> str:  # pragma: no cover
        return "Common Settings"

    class Meta:
        verbose_name = "Common Settings"
        verbose_name_plural = "Common Settings"


class EmailLog(TimeStampedModel):
    to = models.EmailField(db_index=True)
    subject = models.TextField(db_index=True)
    sent_at = models.DateTimeField(auto_now_add=True, db_index=True)
    test_only = models.BooleanField(default=False, db_index=True)
    compressed_body = models.BinaryField(null=True, blank=True)
    compressed_html = models.BinaryField(null=True, blank=True)

    def set_body(self, body: str) -> None:
        """Compress and set text."""
        self.compressed_body = gzip.compress(body.encode())

    def set_html(self, html_body: str) -> None:
        """Compress and set html."""
        self.compressed_html = gzip.compress(html_body.encode())

    @property
    def body(self) -> str | None:
        """Decompress and return text."""
        if self.compressed_body:
            return gzip.decompress(self.compressed_body).decode()
        return None

    @property
    def html(self) -> str | None:
        """Decompress and return html."""
        if self.compressed_html:
            return gzip.decompress(self.compressed_html).decode()
        return None

    def __str__(self) -> str:
        return f"Email to: {self.to}"

    class Meta:
        indexes = [
            models.Index(fields=["to", "sent_at", "test_only"], name="ix_emaillog_to_sentat_testonly"),
        ]


# ---- Tag Models ----


class Tag(TimeStampedModel):
    name = models.CharField(max_length=64, unique=True, db_index=True)
    description = models.TextField(blank=True, null=True)
    color = models.CharField(max_length=7, blank=True, null=True, help_text="Hex color (e.g. #FF0099)")
    icon = models.CharField(max_length=64, blank=True, null=True, help_text="Optional icon name")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children")

    def clean(self) -> None:
        """Ensure stripping whitespace."""
        if self.name:
            self.name = self.name.strip()

    def __str__(self) -> str:
        return self.name


class TagAssignment(TimeStampedModel):
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name="assignments")
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.UUIDField()
    content_object = GenericForeignKey("content_type", "object_id")
    assigned_by = models.UUIDField(null=True, blank=True)  # Could link to user, if wanted

    class Meta:
        unique_together = ("tag", "content_type", "object_id")
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.tag} -> {self.content_object}"


# ---- Taggable Mixin ----


class TagManager:
    def __init__(self, instance: models.Model):
        """Init."""
        self.instance = instance

    def all(self) -> t.List[Tag]:
        """Return all tags."""
        return [ta.tag for ta in self.instance.tags.all()]  # type: ignore[attr-defined]

    def add(self, *names: str) -> None:
        """Add the tags."""
        from django.contrib.contenttypes.models import ContentType  # local import to avoid circularity
        from django.db.models import Q

        from common.utils import get_or_create_with_race_protection

        ct = ContentType.objects.get_for_model(self.instance.__class__)
        for name in names:
            name = name.strip()
            if not name:
                continue  # skip blanks
            tag, _ = get_or_create_with_race_protection(Tag, Q(name=name), {"name": name})
            TagAssignment.objects.get_or_create(
                tag=tag,
                content_type=ct,
                object_id=self.instance.pk,
            )

    def remove(self, *names: str) -> None:
        """Remove the tags."""
        from django.contrib.contenttypes.models import ContentType

        ct = ContentType.objects.get_for_model(self.instance.__class__)
        TagAssignment.objects.filter(
            tag__name__in=names,
            content_type=ct,
            object_id=self.instance.pk,
        ).delete()

    def clear(self) -> None:
        """Delete all tags."""
        from django.contrib.contenttypes.models import ContentType

        ct = ContentType.objects.get_for_model(self.instance.__class__)
        TagAssignment.objects.filter(
            content_type=ct,
            object_id=self.instance.pk,
        ).delete()


class TaggableMixin(models.Model):
    tags = GenericRelation(
        TagAssignment,
        related_query_name="%(class)s",
        content_type_field="content_type",
        object_id_field="object_id",
    )

    class Meta:
        abstract = True

    @property
    def tags_manager(self) -> TagManager:
        """Helper to get the tag manager."""
        return TagManager(self)

    def add_tags(self, *names: str) -> None:
        """Add tags."""
        self.tags_manager.add(*names)

    def remove_tags(self, *names: str) -> None:
        """Remove tags."""
        self.tags_manager.remove(*names)

    def clear_tags(self) -> None:
        """Clear tags."""
        self.tags_manager.clear()

    def get_tags(self) -> t.List[Tag]:
        """Get tags."""
        return self.tags_manager.all()


class FileUploadAudit(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "PENDING"
        CLEAN = "CLEAN"
        MALICIOUS = "MALICIOUS"

    app = models.CharField(max_length=64, db_index=True)
    model = models.CharField(max_length=64, db_index=True)
    instance_pk = models.UUIDField(db_index=True)
    field = models.CharField(max_length=64, db_index=True)
    file_hash = models.CharField(max_length=64, db_index=True)
    uploader = models.EmailField(db_index=True)
    status = models.CharField(choices=Status.choices, max_length=20, db_index=True, default=Status.PENDING)
    notified = models.BooleanField(default=False)


class QuarantinedFile(TimeStampedModel):
    audit = models.OneToOneField(FileUploadAudit, on_delete=models.CASCADE)
    file = models.FileField(upload_to="quarantined_files")
    findings = models.JSONField()
