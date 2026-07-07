from django.conf import settings
from django.contrib.gis.db import models
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import MinValueValidator

from common.fields import MarkdownField, ProtectedFileField
from common.models import TimeStampedModel

from .event_series import EventSeries
from .mixins import VisibilityMixin
from .ticket import TicketTier


class SeriesPass(TimeStampedModel, VisibilityMixin):
    """A season-ticket product on an EventSeries.

    Covers the events mapped via SeriesPassTierLink. Price decreases by
    ``pro_rata_discount`` for each covered event that has already started.
    """

    event_series = models.ForeignKey(EventSeries, on_delete=models.CASCADE, related_name="series_passes")
    name = models.CharField(max_length=255, db_index=True)
    description = MarkdownField(null=True, blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    pro_rata_discount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    currency = models.CharField(max_length=3, default=settings.DEFAULT_CURRENCY)
    payment_method = models.CharField(
        choices=TicketTier.PaymentMethod.choices,
        default=TicketTier.PaymentMethod.ONLINE,
        max_length=20,
        db_index=True,
    )
    purchasable_by = models.CharField(
        choices=TicketTier.PurchasableBy.choices,
        default=TicketTier.PurchasableBy.PUBLIC,
        max_length=20,
        db_index=True,
    )
    sales_start_at = models.DateTimeField(null=True, blank=True, db_index=True)
    sales_end_at = models.DateTimeField(null=True, blank=True, db_index=True)
    is_active = models.BooleanField(default=True)
    total_quantity = models.PositiveIntegerField(default=None, null=True, blank=True)
    quantity_sold = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event_series", "name"], name="unique_series_pass_name"),
        ]
        ordering = ["event_series", "name"]

    def __str__(self) -> str:
        return f"{self.name} for series {self.event_series.name}"

    def clean(self) -> None:
        """Reject unsupported payment methods and inconsistent purchasable_by values."""
        super().clean()
        if self.payment_method == TicketTier.PaymentMethod.AT_THE_DOOR:
            raise DjangoValidationError({"payment_method": "At-the-door payment is not supported for series passes."})
        if self.purchasable_by in (
            TicketTier.PurchasableBy.INVITED,
            TicketTier.PurchasableBy.INVITED_AND_MEMBERS,
        ):
            raise DjangoValidationError({"purchasable_by": "Series passes cannot be invitation-restricted."})


class SeriesPassTierLink(TimeStampedModel):
    """Maps a SeriesPass to one existing tier per covered event."""

    series_pass = models.ForeignKey(SeriesPass, on_delete=models.CASCADE, related_name="tier_links")
    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="series_pass_links")
    tier = models.ForeignKey(TicketTier, on_delete=models.CASCADE, related_name="series_pass_links")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["series_pass", "event"], name="unique_series_pass_event"),
        ]

    def __str__(self) -> str:
        return f"{self.series_pass_id} -> {self.event_id} ({self.tier_id})"

    def clean(self) -> None:
        """Validate the tier/event/series/currency/seating consistency contract."""
        super().clean()
        if self.tier.event_id != self.event_id:
            raise DjangoValidationError({"tier": "Tier must belong to the covered event."})
        if self.event.event_series_id != self.series_pass.event_series_id:
            raise DjangoValidationError({"event": "Event must belong to the pass's series."})
        if self.tier.currency != self.series_pass.currency:
            raise DjangoValidationError({"tier": "Tier currency must match the pass currency."})
        if self.tier.seat_assignment_mode != TicketTier.SeatAssignmentMode.NONE:
            raise DjangoValidationError({"tier": "Assigned-seating tiers cannot back a series pass."})


class HeldSeriesPass(TimeStampedModel):
    """A user's purchased series pass. Its id is the QR payload (``series:<uuid>``)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACTIVE = "active", "Active"
        CANCELLED = "cancelled", "Cancelled"

    series_pass = models.ForeignKey(SeriesPass, on_delete=models.PROTECT, related_name="held_passes")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="held_series_passes")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    price_paid = models.DecimalField(max_digits=10, decimal_places=2)
    stripe_session_id = models.CharField(max_length=255, blank=True, default="", db_index=True)

    pdf_file = ProtectedFileField(upload_to="series_passes/pdf/", null=True, blank=True)
    pkpass_file = ProtectedFileField(upload_to="series_passes/pkpass/", null=True, blank=True)
    file_content_hash = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["series_pass", "user"],
                condition=~models.Q(status="cancelled"),
                name="unique_active_held_pass_per_user",
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"HeldSeriesPass {self.id} ({self.user_id})"
