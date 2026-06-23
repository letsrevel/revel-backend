"""Schemas for recurrence rules."""

from uuid import UUID

from ninja import Schema
from pydantic import AwareDatetime, Field, model_validator

from events.models.recurrence_rule import RecurrenceRule
from events.utils import recurrence_validators


class RecurrenceRuleCreateSchema(Schema):
    """Schema for creating a recurrence rule."""

    frequency: RecurrenceRule.Frequency
    interval: int = Field(default=1, ge=1)
    weekdays: list[int] = []
    monthly_type: RecurrenceRule.MonthlyType | None = None
    day_of_month: int | None = None
    nth_weekday: int | None = None
    weekday: int | None = None
    dtstart: AwareDatetime
    until: AwareDatetime | None = None
    count: int | None = Field(default=None, ge=1)
    timezone: str = Field(
        default="UTC",
        description=(
            "IANA timezone name the recurrence anchor is localized to. "
            "Occurrences preserve their wall-clock time-of-day in this zone "
            "across DST transitions: a weekly rule for 'Mondays at 10:00 "
            "Europe/Vienna' materializes at 10:00 Vienna time both before and "
            "after the spring/autumn switch (the underlying UTC instant shifts "
            "by the DST offset). `dtstart` is the UTC instant of the first "
            "occurrence; its wall-clock time in this zone defines the anchor. "
            "The default 'UTC' has no DST, so occurrences stay fixed to the "
            "stored UTC instant."
        ),
    )

    @model_validator(mode="after")
    def validate_recurrence(self) -> "RecurrenceRuleCreateSchema":
        """Validate field combinations via the shared helpers.

        ``RecurrenceValidationError`` subclasses ``ValueError`` so Pydantic
        propagates the field-scoped message as a normal validation error.
        """
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
        return self


class RecurrenceRuleUpdateSchema(Schema):
    """Schema for updating a recurrence rule — all fields optional.

    ``dtstart`` is intentionally excluded. Changing the recurrence anchor
    date of a live series would require re-materializing all future
    occurrences (and deciding what to do with already-sold tickets on the
    existing ones). This is a destructive operation that belongs on a
    dedicated "rebase series" workflow, not on a generic PATCH.
    """

    frequency: RecurrenceRule.Frequency | None = None
    interval: int | None = Field(default=None, ge=1)
    weekdays: list[int] | None = None
    monthly_type: RecurrenceRule.MonthlyType | None = None
    day_of_month: int | None = None
    nth_weekday: int | None = None
    weekday: int | None = None
    until: AwareDatetime | None = None
    count: int | None = Field(default=None, ge=1)
    timezone: str | None = None

    @model_validator(mode="after")
    def validate_partial_update(self) -> "RecurrenceRuleUpdateSchema":
        """Validate per-field ranges and the until/count exclusivity rule.

        Cross-field rules that depend on the persisted model state (e.g.
        "monthly_type=day requires day_of_month") are intentionally not
        enforced here — only the model's ``clean()`` has access to the merged
        instance and remains the canonical check. This validator catches the
        common API-boundary mistakes (out-of-range fields, both until and
        count set, invalid weekday list, unknown timezone) so callers get a
        clean 422 instead of a 500 deep in the service layer.
        """
        if self.weekdays is not None:
            recurrence_validators.validate_weekdays(self.weekdays)
        recurrence_validators.validate_monthly_field_ranges(
            day_of_month=self.day_of_month,
            nth_weekday=self.nth_weekday,
            weekday=self.weekday,
        )
        if self.until is not None and self.count is not None:
            recurrence_validators.validate_boundaries(None, self.until, self.count)
        if self.timezone is not None:
            recurrence_validators.validate_timezone(self.timezone)
        return self


class RecurrenceRuleSchema(Schema):
    """Retrieve schema for a recurrence rule."""

    id: UUID
    frequency: RecurrenceRule.Frequency
    interval: int
    weekdays: list[int]
    monthly_type: RecurrenceRule.MonthlyType | None = None
    day_of_month: int | None = None
    nth_weekday: int | None = None
    weekday: int | None = None
    dtstart: AwareDatetime
    until: AwareDatetime | None = None
    count: int | None = None
    timezone: str
    rrule_string: str
