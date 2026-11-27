"""Abstract base models and mixins shared across the platform."""

import typing as t
import uuid

from django.db import models
from django.db.models import Q
from django.db.models.fields.mixins import FieldCacheMixin


def _deferred_fields_of_full_save(instance: models.Model, kwargs: dict[str, t.Any]) -> set[str]:
    """The deferred fields (``.only()``/``.defer()``) of an instance that is saved without ``update_fields``.

    Django writes only the loaded fields of such an instance, and it finds them at the end of the save chain. But a
    read of a deferred field loads it: ``full_clean()`` would read every deferred field first, one query each, and
    Django would then write every column, also one that another request changed meanwhile (#1572). Empty when the
    save is not such a save.
    """
    if kwargs.get("update_fields") is not None or instance._state.adding or kwargs.get("force_insert"):
        return set()
    return instance.get_deferred_fields()


class TimeStampedModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        abstract = True

    def save(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Validate the row, then save it.

        A full save runs ``full_clean()``. A save with ``update_fields`` checks only the fields that it writes
        with ``clean_fields``, so an unrelated field (for example an image file that is missing from storage)
        cannot fail a counter or status update (#1563). ``clean()``, the unique checks and the constraints still
        run on the whole row: a cross-field rule that an earlier edit broke still blocks the save, and its error
        names the field to fix.

        A full save of a deferred instance checks only the loaded fields. Its unique and constraint checks skip
        the fields that were not loaded; the database constraints still apply to them. Then the fields that the
        checks read become deferred again, so Django writes only the loaded fields and the fields that a later
        save step (for example ``LocationMixin``) sets (#1572). As in Django, ``updated_at`` is written only when
        it was loaded. A change that ``clean()`` makes to a field that was not loaded is not written, the same as
        for an explicit ``update_fields``.
        """
        deferred = _deferred_fields_of_full_save(self, kwargs)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            # A list, so that a one-shot iterator is not used up here and then reaches Django empty.
            kwargs["update_fields"] = update_fields = list(update_fields)
            if not update_fields:
                super().save(*args, **kwargs)  # Django writes nothing for an empty list, so validate nothing
                return
        if update_fields is None and not deferred:
            self.full_clean()
        else:
            written = set(update_fields) if update_fields is not None else set()
            unchanged = [
                f.name
                for f in self._meta.concrete_fields
                if f.attname in deferred
                or (update_fields is not None and f.name not in written and f.attname not in written)
            ]
            try:
                self.full_clean(exclude=unchanged, validate_unique=False, validate_constraints=False)
                # A check on a field that was not loaded is skipped (the database constraint still applies): else
                # the check reads every deferred field, one query each.
                not_loaded = {f.name for f in self._meta.concrete_fields if f.attname in deferred}
                self.validate_unique(exclude=not_loaded)
                self.validate_constraints(exclude=not_loaded)
            finally:
                # Also when a check fails: a retry of the same instance must not write a field that a check read.
                self._defer_again(deferred)
        super().save(*args, **kwargs)

    def _defer_again(self, deferred: set[str]) -> None:
        """Defer again the fields that a check read, so Django does not write them (#1572)."""
        for field in self._meta.concrete_fields:
            if field.attname in deferred and field.attname in self.__dict__:
                del self.__dict__[field.attname]
                # A cached related object would make Django read the column again before the write.
                if isinstance(field, FieldCacheMixin) and field.is_cached(self):
                    field.delete_cached_value(self)


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

        The write is a DB-side compare-and-set (``email_sent_at__isnull=True``) via
        ``_base_manager.update``, so two concurrent workers can't both win and it
        never trips ``full_clean`` on an org_deleted document's null org FK.
        """
        if self.email_sent_at is not None:
            return
        from django.utils import timezone

        now = timezone.now()
        values: dict[str, t.Any] = {
            "email_sent_at": now,
            "email_delivery_failed_at": None,
            "email_delivery_error": "",
        }
        if hasattr(self, "updated_at"):
            values["updated_at"] = now
        updated = type(self)._base_manager.filter(pk=self.pk, email_sent_at__isnull=True).update(**values)
        if updated:
            self.email_sent_at = now
            self.email_delivery_failed_at = None
            self.email_delivery_error = ""
            if hasattr(self, "updated_at"):
                self.updated_at = now

    def mark_email_undeliverable(self, reason: "EmailDeliverableMixin.DeliveryFailureReason") -> None:
        """Record a terminal delivery failure (idempotent — no-op once sent or already failed).

        Excludes the document from the recovery sweep so a genuinely undeliverable
        document isn't re-enqueued every sweep forever (issue #618).

        The write is a DB-side compare-and-set (both terminal fields still null) via
        ``_base_manager.update``: it loses cleanly to a concurrent ``mark_email_sent``
        (never leaving both fields populated) and doesn't trip ``full_clean`` on the
        now-null (but non-blank) organization FK.
        """
        if self.email_sent_at is not None or self.email_delivery_failed_at is not None:
            return
        from django.utils import timezone

        now = timezone.now()
        values: dict[str, t.Any] = {"email_delivery_failed_at": now, "email_delivery_error": reason}
        if hasattr(self, "updated_at"):
            values["updated_at"] = now
        updated = (
            type(self)
            ._base_manager.filter(pk=self.pk, email_sent_at__isnull=True, email_delivery_failed_at__isnull=True)
            .update(**values)
        )
        if updated:
            self.email_delivery_failed_at = now
            self.email_delivery_error = reason
            if hasattr(self, "updated_at"):
                self.updated_at = now

    def clear_email_undeliverable(self) -> None:
        """Clear the terminal-failure state so the document re-enters the recovery sweep.

        Used by the admin retry action once an operator fixes the recipient. Written
        via ``_base_manager.update`` (no ``full_clean``) so an org_deleted document's
        null org FK doesn't block the clear.
        """
        if self.email_delivery_failed_at is None:
            return
        from django.utils import timezone

        now = timezone.now()
        values: dict[str, t.Any] = {"email_delivery_failed_at": None, "email_delivery_error": ""}
        if hasattr(self, "updated_at"):
            values["updated_at"] = now
        type(self)._base_manager.filter(pk=self.pk).update(**values)
        self.email_delivery_failed_at = None
        self.email_delivery_error = ""
        if hasattr(self, "updated_at"):
            self.updated_at = now


# ORM equivalent of ``StripeConnectMixin.is_stripe_connected``, for filtering and
# counting across a queryset (the property itself is unreachable from the ORM).
# Keep the two in lockstep.
STRIPE_CONNECTED_Q = Q(
    stripe_account_id__isnull=False,
    stripe_charges_enabled=True,
    stripe_details_submitted=True,
)


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

    def _strip_exif_from_image_fields(self, update_fields: t.Iterable[str] | None = None) -> None:
        from django.core.exceptions import ValidationError
        from django.utils.translation import gettext_lazy as _
        from PIL import UnidentifiedImageError

        from common.utils import strip_exif

        # A partial save writes only update_fields, so only those images are processed (#1563).
        written = None if update_fields is None else set(update_fields)
        for field_name in self.IMAGE_FIELDS:
            if (written is not None and field_name not in written) or field_name in self.get_deferred_fields():
                continue  # a deferred image is a stored file, not a new upload: do not load it (#1572)
            file = getattr(self, field_name, None)
            # Only strip files not yet committed to storage (fresh uploads).
            # Committed files were already stripped on upload; re-stripping marks
            # the field dirty, so every save() would write a re-encoded duplicate.
            if file and not getattr(file, "_committed", True):
                try:
                    setattr(self, field_name, strip_exif(file))
                except (UnidentifiedImageError, OSError) as e:
                    # Convert PIL errors to ValidationError for proper 400 response.
                    # This is a safety net - validation should happen before save().
                    raise ValidationError({field_name: [_("File is not a valid image.")]}) from e

    def save(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Override save to auto-strip exif from image fields (only those in ``update_fields``, if given)."""
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            # A list, so that a one-shot iterator is not used up here and then reaches Django empty.
            kwargs["update_fields"] = update_fields = list(update_fields)
        self._strip_exif_from_image_fields(update_fields)
        super().save(*args, **kwargs)

    class Meta:
        abstract = True
