"""Recurrence rule model for recurring event series."""

import typing as t
from datetime import datetime

from dateutil.rrule import DAILY, MONTHLY, WEEKLY, YEARLY, rrule
from dateutil.rrule import weekday as rrule_weekday
from django.contrib.gis.db import models
from django.core.exceptions import ValidationError

from common.models import TimeStampedModel
from events.utils import recurrence_validators


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

    # Timezone for the recurrence anchor. Currently reserved metadata: occurrences
    # are anchored to the UTC ``dtstart`` and do not observe DST in the named tz
    # (Phase 3 will use this field to localize the anchor). Validated as a real
    # IANA zone name so bad data is rejected on save.
    timezone = models.CharField(max_length=64, default="UTC")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.get_frequency_display()} (every {self.interval})"

    def clean(self) -> None:
        """Validate recurrence rule fields via shared helpers.

        Delegates each check to ``events.utils.recurrence_validators`` so that
        the Pydantic input schema and this model enforce the exact same rules.
        ``RecurrenceValidationError`` is re-wrapped in Django's
        ``ValidationError`` with field attribution for admin/form callers.
        """
        super().clean()
        try:
            recurrence_validators.validate_weekdays(self.weekdays)
            recurrence_validators.validate_monthly_fields(
                frequency=self.frequency,
                monthly_type=self.monthly_type,
                day_of_month=self.day_of_month,
                nth_weekday=self.nth_weekday,
                weekday=self.weekday,
            )
            recurrence_validators.validate_boundaries(self.dtstart, self.until, self.count)
            recurrence_validators.validate_timezone(self.timezone)
        except recurrence_validators.RecurrenceValidationError as exc:
            if exc.field:
                raise ValidationError({exc.field: exc.message}) from exc
            raise ValidationError(exc.message) from exc

    def to_rrule(self) -> rrule:
        """Build a dateutil rrule object from the stored fields."""
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
        """Compute rrule_string before saving.

        If the caller passes ``update_fields``, ``rrule_string`` is added to it
        automatically so the recomputed value is persisted. Otherwise a save
        that updates ``frequency`` alone would leave ``rrule_string`` stale on
        disk.
        """
        self.rrule_string = str(self.to_rrule())
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            update_fields = set(update_fields)
            update_fields.add("rrule_string")
            kwargs["update_fields"] = update_fields
        super().save(*args, **kwargs)

    def get_occurrences(self, after: datetime, before: datetime) -> list[datetime]:
        """Return occurrence datetimes strictly between ``after`` and ``before``.

        Both endpoints are exclusive: ``after < occurrence < before``.
        """
        rule = self.to_rrule()
        return list(rule.between(after, before, inc=False))
