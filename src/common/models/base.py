"""Abstract base models and mixins shared across the platform."""

import typing as t
import uuid

from django.db import models


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


class EmailDeliverableMixin(models.Model):
    """Mixin for financial documents (invoices, statements) delivered by email.

    Persists a successful-delivery timestamp so a document whose email send
    exhausts its Celery retries isn't silently lost: a recovery sweep can
    re-select documents with ``email_sent_at`` still null and re-dispatch them.

    Delivery is at-least-once by design — :meth:`mark_email_sent` is a no-op once
    set, so re-sending after a partial failure never moves the recorded delivery
    time. See issue #616.
    """

    email_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the document was successfully emailed to its recipient.",
    )

    class Meta:
        abstract = True

    def mark_email_sent(self) -> None:
        """Record a successful email delivery (idempotent — first write wins)."""
        if self.email_sent_at is not None:
            return
        from django.utils import timezone

        self.email_sent_at = timezone.now()
        update_fields = ["email_sent_at"]
        if hasattr(self, "updated_at"):
            update_fields.append("updated_at")
        self.save(update_fields=update_fields)


class StripeConnectMixin(models.Model):
    """Mixin for models that can connect a Stripe account (e.g., Organization, RevelUser).

    Provides the four Stripe Connect fields and an ``is_stripe_connected`` property.
    """

    stripe_account_email = models.EmailField(null=True, blank=True, db_index=True)
    stripe_account_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        unique=True,
        help_text="The Stripe Connect Account ID.",
    )
    stripe_charges_enabled = models.BooleanField(default=False)
    stripe_details_submitted = models.BooleanField(default=False)

    class Meta:
        abstract = True

    @property
    def is_stripe_connected(self) -> bool:
        """Check if the Stripe account is fully connected."""
        return self.stripe_account_id is not None and self.stripe_charges_enabled and self.stripe_details_submitted

    def _stripe_update_fields(self, *fields: str) -> list[str]:
        """Build an ``update_fields`` list, appending ``updated_at`` when the model has it.

        Models inheriting from ``TimeStampedModel`` have ``auto_now=True`` on
        ``updated_at``, which is only honoured when it appears in ``update_fields``.
        ``RevelUser`` (via ``AbstractUser``) does not have this field.
        """
        result = list(fields)
        if hasattr(self, "updated_at"):
            result.append("updated_at")
        return result


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
