import typing as t

from django.db import models

from common.models import TimeStampedModel

from .mixins import LocationMixin, SlugFromNameMixin
from .organization import Organization


class VenueQuerySet(models.QuerySet["Venue"]):
    """Custom queryset for Venue model with prefetch patterns."""

    def with_sectors(self) -> t.Self:
        """Prefetch related sectors."""
        return self.prefetch_related("sectors")

    def with_seats(self) -> t.Self:
        """Prefetch sectors and their seats."""
        return self.prefetch_related("sectors__seats")

    def full(self) -> t.Self:
        """Prefetch all related objects (sectors and seats)."""
        return self.prefetch_related("sectors", "sectors__seats")


class VenueManager(models.Manager["Venue"]):
    """Custom manager for Venue with convenience methods for related object selection."""

    def get_queryset(self) -> VenueQuerySet:
        """Get base queryset."""
        return VenueQuerySet(self.model, using=self._db)

    def with_sectors(self) -> VenueQuerySet:
        """Returns a queryset with sectors prefetched."""
        return self.get_queryset().with_sectors()

    def with_seats(self) -> VenueQuerySet:
        """Returns a queryset with sectors and seats prefetched."""
        return self.get_queryset().with_seats()

    def full(self) -> VenueQuerySet:
        """Returns a queryset with all related objects prefetched."""
        return self.get_queryset().full()


class Venue(SlugFromNameMixin, TimeStampedModel, LocationMixin):
    """A physical venue belonging to an organization.

    Layout (sectors/seats) is optional and FE-defined.

    Note:
        The `capacity` field is informational only and is NOT enforced during
        ticket sales. Actual capacity enforcement happens via:
        - Event.max_attendees (enforced during ticket purchase)
        - TicketTier.total_quantity (enforced during ticket purchase)
        - Materialized seats in sectors (enforced for seated events)
    """

    # Slug must be unique per organization, same convention as Event
    slug_scope_field = "organization"

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="venues",
        db_index=True,
    )

    name = models.CharField(max_length=255, db_index=True)
    slug = models.SlugField(max_length=255)

    description = models.TextField(blank=True, null=True)

    # Allows GA-only venues
    capacity = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Informational only. Not enforced during ticket sales.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "slug"],
                name="unique_organization_venue_slug",
            )
        ]
        ordering = ["name"]
        indexes = [
            models.Index(fields=["organization", "name"]),
        ]

    objects = VenueManager()

    def __str__(self) -> str:
        return f"{self.name} ({self.organization.name})"


class VenueSector(TimeStampedModel):
    """A logical area inside a venue (e.g. Balcony, Floor Left).

    Note:
        The `capacity` field is informational only and is NOT enforced during
        ticket sales. For seated events, capacity is implicitly enforced by the
        number of materialized VenueSeat objects in this sector.
    """

    venue = models.ForeignKey(
        Venue,
        on_delete=models.CASCADE,
        related_name="sectors",
        db_index=True,
    )

    name = models.CharField(max_length=100, db_index=True)
    code = models.CharField(max_length=30, blank=True, null=True)

    shape = models.JSONField(
        null=True,
        blank=True,
        help_text="Arbitrary polygon for FE rendering (list of points: [[x,y],...]).",
    )

    capacity = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Informational only. Actual capacity enforced by materialized seats.",
    )

    # Controls ordering in FE lists
    display_order = models.PositiveIntegerField(default=0, db_index=True)

    # Arbitrary metadata for FE rendering (e.g., aisle positions, labels, etc.)
    metadata = models.JSONField(
        null=True,
        blank=True,
        default=None,
        help_text="Arbitrary JSON metadata for frontend rendering (e.g., aisle positions).",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["venue", "name"],
                name="unique_venue_sector_name",
            )
        ]
        ordering = ["venue", "display_order", "name"]
        indexes = [
            models.Index(fields=["venue", "display_order"]),
            models.Index(fields=["venue", "name"]),
        ]

    def __str__(self) -> str:
        return f"{self.venue.name}: {self.name}"


class VenueSeat(TimeStampedModel):
    """A materialized seat inside a sector.

    Used in reserved seating, or as 'spots' in standing sections.
    """

    sector = models.ForeignKey(
        VenueSector,
        on_delete=models.CASCADE,
        related_name="seats",
        db_index=True,
    )

    label = models.CharField(max_length=50, db_index=True)

    # Optional structured labeling
    row = models.CharField(max_length=20, blank=True, null=True, db_index=True)
    number = models.PositiveIntegerField(null=True, blank=True, db_index=True)

    position = models.JSONField(
        null=True,
        blank=True,
        help_text="Seat position for FE rendering (pixel-based or relative coordinate).",
    )

    is_accessible = models.BooleanField(default=False)
    is_obstructed_view = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["sector", "label"],
                name="unique_sector_seat_label",
            )
        ]
        ordering = ["sector", "row", "number", "label"]
        indexes = [
            models.Index(fields=["sector", "row", "number"]),
            models.Index(fields=["sector", "label"]),
            models.Index(fields=["sector", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.sector.name} / {self.label}"
