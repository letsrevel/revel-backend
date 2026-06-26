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

    A document with no resolvable recipient (or whose org was deleted) can never
    be delivered, so retrying it forever is pointless noise. :meth:`mark_email_undeliverable`
    records a terminal failure state that the recovery sweep excludes, so such a
    document is enqueued once and then left alone until an operator fixes the
    recipient and retries it. See issue #618.
    """

    class DeliveryFailureReason(models.TextChoices):
        NO_RECIPIENT = "no_recipient", "No resolvable recipient"
        ORG_DELETED = "org_deleted", "Organization deleted"

    email_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="When the document was successfully emailed to its recipient.",
    )
    email_delivery_failed_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="When the document was found permanently undeliverable (no recipient / org gone).",
    )
    email_delivery_error = models.CharField(
        max_length=32,
        blank=True,
        choices=DeliveryFailureReason.choices,
        help_text="Why the document is permanently undeliverable.",
    )

    class Meta:
        abstract = True

    def mark_email_sent(self) -> None:
        """Record a successful email delivery (idempotent — first write wins).

        Also clears any prior terminal-failure state so a document that became
        deliverable (e.g. an operator fixed the recipient) leaves the undeliverable
        set on its next successful send.
        """
        if self.email_sent_at is not None:
            return
        from django.utils import timezone

        self.email_sent_at = timezone.now()
        update_fields = ["email_sent_at"]
        if self.email_delivery_failed_at is not None:
            self.email_delivery_failed_at = None
            self.email_delivery_error = ""
            update_fields += ["email_delivery_failed_at", "email_delivery_error"]
        if hasattr(self, "updated_at"):
            update_fields.append("updated_at")
        self.save(update_fields=update_fields)

    def mark_email_undeliverable(self, reason: "EmailDeliverableMixin.DeliveryFailureReason") -> None:
        """Record a terminal delivery failure (idempotent — no-op once sent or already failed).

        Excludes the document from the recovery sweep so a genuinely undeliverable
        document isn't re-enqueued every sweep forever (issue #618).

        Written via ``QuerySet.update`` rather than ``save()`` so the ``org_deleted``
        case doesn't trip ``full_clean`` on the now-null (but non-blank) organization FK.
        """
        if self.email_sent_at is not None or self.email_delivery_failed_at is not None:
            return
        from django.utils import timezone

        now = timezone.now()
        self.email_delivery_failed_at = now
        self.email_delivery_error = reason
        values: dict[str, t.Any] = {"email_delivery_failed_at": now, "email_delivery_error": reason}
        if hasattr(self, "updated_at"):
            self.updated_at = now
            values["updated_at"] = now
        type(self)._base_manager.filter(pk=self.pk).update(**values)


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
