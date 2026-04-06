"""Schemas for recurrence rules."""

from uuid import UUID

from ninja import Schema
from pydantic import AwareDatetime, model_validator

from events.models.recurrence_rule import RecurrenceRule


class RecurrenceRuleCreateSchema(Schema):
    """Schema for creating a recurrence rule."""

    frequency: RecurrenceRule.Frequency
    interval: int = 1
    weekdays: list[int] = []
    monthly_type: RecurrenceRule.MonthlyType | None = None
    day_of_month: int | None = None
    nth_weekday: int | None = None
    weekday: int | None = None
    dtstart: AwareDatetime
    until: AwareDatetime | None = None
    count: int | None = None
    timezone: str = "UTC"

    @model_validator(mode="after")
    def validate_recurrence(self) -> "RecurrenceRuleCreateSchema":
        """Validate field combinations."""
        if self.until and self.count:
            raise ValueError("Cannot set both 'until' and 'count'.")

        for day in self.weekdays:
            if day < 0 or day > 6:
                raise ValueError("Each weekday must be 0 (Monday) to 6 (Sunday).")

        if self.frequency == RecurrenceRule.Frequency.MONTHLY:
            self._validate_monthly_fields()

        return self

    def _validate_monthly_fields(self) -> None:
        if not self.monthly_type:
            raise ValueError("monthly_type is required for monthly recurrence.")
        if self.monthly_type == RecurrenceRule.MonthlyType.DAY_OF_MONTH:
            if not self.day_of_month or self.day_of_month < 1 or self.day_of_month > 31:
                raise ValueError("day_of_month must be between 1 and 31.")
        elif self.monthly_type == RecurrenceRule.MonthlyType.NTH_WEEKDAY:
            if self.nth_weekday is None or self.nth_weekday not in (-1, 1, 2, 3, 4):
                raise ValueError("nth_weekday must be 1-4 or -1 (last).")
            if self.weekday is None or self.weekday < 0 or self.weekday > 6:
                raise ValueError("weekday must be 0 (Monday) to 6 (Sunday).")


class RecurrenceRuleUpdateSchema(Schema):
    """Schema for updating a recurrence rule — all fields optional."""

    frequency: RecurrenceRule.Frequency | None = None
    interval: int | None = None
    weekdays: list[int] | None = None
    monthly_type: RecurrenceRule.MonthlyType | None = None
    day_of_month: int | None = None
    nth_weekday: int | None = None
    weekday: int | None = None
    until: AwareDatetime | None = None
    count: int | None = None
    timezone: str | None = None


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
