"""Recurrence rule model for recurring event series."""

import typing as t

from dateutil.rrule import DAILY, MONTHLY, WEEKLY, YEARLY, rrule
from django.contrib.gis.db import models
from django.core.exceptions import ValidationError

from common.models import TimeStampedModel


class RecurrenceRule(TimeStampedModel):
    """Defines a recurrence pattern for generating event occurrences.

    Stores recurrence parameters (frequency, interval, weekdays, etc.) and
    computes an RFC 5545 RRULE string on save via python-dateutil.
    """

    class Frequency(models.TextChoices):
        DAILY = "daily"
        WEEKLY = "weekly"
        MONTHLY = "monthly"
        YEARLY = "yearly"

    class MonthlyType(models.TextChoices):
        DAY_OF_MONTH = "day", "Day of month"
        NTH_WEEKDAY = "weekday", "Nth weekday"

    FREQ_MAP: t.ClassVar[dict[str, int]] = {
        Frequency.DAILY: DAILY,
        Frequency.WEEKLY: WEEKLY,
        Frequency.MONTHLY: MONTHLY,
        Frequency.YEARLY: YEARLY,
    }

    frequency = models.CharField(choices=Frequency.choices, max_length=10)
    interval = models.PositiveIntegerField(default=1)

    # For weekly recurrence: list of ints 0=Monday .. 6=Sunday
    weekdays = models.JSONField(default=list, blank=True)

    # For monthly recurrence
    monthly_type = models.CharField(
        choices=MonthlyType.choices,
        max_length=10,
        null=True,
        blank=True,
    )
    day_of_month = models.PositiveIntegerField(null=True, blank=True)
    nth_weekday = models.IntegerField(null=True, blank=True)  # 1-4 or -1 for last
    weekday = models.IntegerField(null=True, blank=True)  # 0-6

    # Boundaries
    dtstart = models.DateTimeField()
    until = models.DateTimeField(null=True, blank=True)
    count = models.PositiveIntegerField(null=True, blank=True)

    # Computed RRULE string (read-only, populated on save)
    rrule_string = models.TextField(editable=False, blank=True, default="")

    # Timezone for the recurrence anchor
    timezone = models.CharField(max_length=64, default="UTC")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.get_frequency_display()} (every {self.interval})"

    def clean(self) -> None:
        """Validate recurrence rule fields."""
        super().clean()
        self._validate_weekdays()
        self._validate_monthly_fields()
        self._validate_boundaries()

    def _validate_weekdays(self) -> None:
        if not self.weekdays:
            return
        if not isinstance(self.weekdays, list):
            raise ValidationError({"weekdays": "Must be a list of integers."})
        for day in self.weekdays:
            if not isinstance(day, int) or day < 0 or day > 6:
                raise ValidationError({"weekdays": "Each day must be an integer 0 (Monday) to 6 (Sunday)."})

    def _validate_monthly_fields(self) -> None:
        if self.frequency != self.Frequency.MONTHLY:
            return
        if not self.monthly_type:
            raise ValidationError({"monthly_type": "Required for monthly recurrence."})
        if self.monthly_type == self.MonthlyType.DAY_OF_MONTH:
            if not self.day_of_month or self.day_of_month < 1 or self.day_of_month > 31:
                raise ValidationError({"day_of_month": "Must be between 1 and 31."})
        elif self.monthly_type == self.MonthlyType.NTH_WEEKDAY:
            if self.nth_weekday is None or self.nth_weekday not in (-1, 1, 2, 3, 4):
                raise ValidationError({"nth_weekday": "Must be 1-4 or -1 (last)."})
            if self.weekday is None or self.weekday < 0 or self.weekday > 6:
                raise ValidationError({"weekday": "Must be 0 (Monday) to 6 (Sunday)."})

    def _validate_boundaries(self) -> None:
        if self.until and self.count:
            raise ValidationError("Cannot set both 'until' and 'count'. Choose one or neither.")
        if self.until and self.until <= self.dtstart:
            raise ValidationError({"until": "Must be after dtstart."})

    def to_rrule(self) -> rrule:
        """Build a dateutil rrule object from the stored fields."""
        from dateutil.rrule import weekday as rrule_weekday

        freq = self.FREQ_MAP[self.frequency]
        kwargs: dict[str, t.Any] = {
            "freq": freq,
            "interval": self.interval,
            "dtstart": self.dtstart,
        }

        if self.until:
            kwargs["until"] = self.until
        if self.count:
            kwargs["count"] = self.count

        if self.frequency == self.Frequency.WEEKLY and self.weekdays:
            kwargs["byweekday"] = [rrule_weekday(d) for d in self.weekdays]

        if self.frequency == self.Frequency.MONTHLY:
            if self.monthly_type == self.MonthlyType.DAY_OF_MONTH and self.day_of_month:
                kwargs["bymonthday"] = self.day_of_month
            elif (
                self.monthly_type == self.MonthlyType.NTH_WEEKDAY
                and self.weekday is not None
                and self.nth_weekday is not None
            ):
                kwargs["byweekday"] = rrule_weekday(self.weekday)(self.nth_weekday)

        return rrule(**kwargs)

    def save(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Compute rrule_string before saving."""
        self.rrule_string = str(self.to_rrule())
        super().save(*args, **kwargs)

    def get_occurrences(self, after: t.Any, before: t.Any) -> list[t.Any]:
        """Return occurrence datetimes between after and before (exclusive)."""
        rule = self.to_rrule()
        return list(rule.between(after, before, inc=False))
