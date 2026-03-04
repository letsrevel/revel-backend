import typing as t

from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from common.models import TimeStampedModel

if t.TYPE_CHECKING:
    from .organization import Organization


class DiscountCodeQuerySet(models.QuerySet["DiscountCode"]):
    """Custom QuerySet for DiscountCode with filtering methods."""

    def active(self) -> t.Self:
        """Return only currently active and valid discount codes."""
        now = timezone.now()
        return (
            self.filter(
                is_active=True,
            )
            .filter(
                models.Q(valid_from__isnull=True) | models.Q(valid_from__lte=now),
            )
            .filter(
                models.Q(valid_until__isnull=True) | models.Q(valid_until__gte=now),
            )
        )

    def for_organization(self, organization: "Organization") -> t.Self:
        """Filter by organization."""
        return self.filter(organization=organization)


class DiscountCodeManager(models.Manager["DiscountCode"]):
    """Custom manager for DiscountCode."""

    def get_queryset(self) -> DiscountCodeQuerySet:
        """Get base queryset."""
        return DiscountCodeQuerySet(self.model, using=self._db)

    def active(self) -> DiscountCodeQuerySet:
        """Return only currently active discount codes."""
        return self.get_queryset().active()

    def for_organization(self, organization: "Organization") -> DiscountCodeQuerySet:
        """Filter by organization."""
        return self.get_queryset().for_organization(organization)


class DiscountCode(TimeStampedModel):
    """A discount code that can be applied to ticket purchases."""

    class DiscountType(models.TextChoices):
        PERCENTAGE = "percentage", "Percentage"
        FIXED_AMOUNT = "fixed_amount", "Fixed Amount"

    code = models.CharField(max_length=64, db_index=True)
    organization = models.ForeignKey(
        "events.Organization",
        on_delete=models.CASCADE,
        related_name="discount_codes",
    )
    discount_type = models.CharField(
        max_length=20,
        choices=DiscountType.choices,
        db_index=True,
    )
    discount_value = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(0)],
    )
    currency = models.CharField(
        max_length=3,
        null=True,
        blank=True,
        help_text="Required for FIXED_AMOUNT discount type. ISO 4217 currency code.",
    )
    valid_from = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the discount code becomes valid. None = immediately valid.",
    )
    valid_until = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the discount code expires. None = no expiry.",
    )
    max_uses = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Maximum total uses. None = unlimited.",
    )
    max_uses_per_user = models.PositiveIntegerField(
        default=1,
        help_text="Maximum uses per user.",
    )
    times_used = models.PositiveIntegerField(
        default=0,
        help_text="Number of times this code has been used. Atomically incremented via F().",
    )
    min_purchase_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
        help_text="Minimum purchase amount (price x quantity) required to use this code.",
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Manual kill switch to deactivate the discount code.",
    )

    # Scope narrowing (M2M, union logic: if any populated, code applies to those entities)
    series = models.ManyToManyField(
        "events.EventSeries",
        blank=True,
        related_name="discount_codes",
    )
    events = models.ManyToManyField(
        "events.Event",
        blank=True,
        related_name="discount_codes",
    )
    tiers = models.ManyToManyField(
        "events.TicketTier",
        blank=True,
        related_name="discount_codes",
    )

    objects = DiscountCodeManager()

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "code"],
                name="unique_org_discount_code",
            ),
        ]

    def clean(self) -> None:
        """Validate discount code fields."""
        from decimal import Decimal

        from django.core.exceptions import ValidationError

        super().clean()

        # Uppercase the code
        if self.code:
            self.code = self.code.upper()

        # Validate date range
        if self.valid_from and self.valid_until and self.valid_from >= self.valid_until:
            raise ValidationError({"valid_until": "valid_until must be after valid_from."})

        # Validate percentage range
        if self.discount_type == self.DiscountType.PERCENTAGE:
            if self.discount_value > Decimal("100"):
                raise ValidationError({"discount_value": "Percentage discount cannot exceed 100."})

        # Validate currency for FIXED_AMOUNT
        if self.discount_type == self.DiscountType.FIXED_AMOUNT and not self.currency:
            raise ValidationError({"currency": "Currency is required for fixed amount discounts."})

    def __str__(self) -> str:
        return f"{self.code} ({self.organization})"
