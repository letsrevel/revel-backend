"""Pure validation helpers for recurrence rule fields.

Shared by the Pydantic ``RecurrenceRuleCreateSchema`` (API input) and the
Django ``RecurrenceRule`` model's ``clean()`` method so validation rules have
a single source of truth. Each helper raises ``RecurrenceValidationError``
with the offending field name (or ``None`` for non-field-specific errors);
callers translate to the exception type their framework expects.
"""

import typing as t
import zoneinfo


class RecurrenceValidationError(ValueError):
    """Raised when a recurrence rule field is invalid.

    Inherits from ``ValueError`` so Pydantic ``model_validator`` hooks can
    raise instances directly. The Django model's ``clean()`` method catches
    and re-wraps in ``django.core.exceptions.ValidationError`` with field
    attribution.
    """

    def __init__(self, field: str | None, message: str) -> None:
        """Initialize with an optional field name and an error message."""
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}" if field else message)


# These string literals intentionally mirror ``RecurrenceRule.Frequency`` and
# ``RecurrenceRule.MonthlyType`` choice values. They are duplicated (rather than
# imported) to keep this module free of Django model imports — it is intended
# to be importable from schemas without triggering app ready.
_MONTHLY_FREQUENCY = "monthly"
_MONTHLY_TYPE_DAY = "day"
_MONTHLY_TYPE_NTH_WEEKDAY = "weekday"


def validate_weekdays(weekdays: t.Any) -> None:
    """Validate ``weekdays`` is a list of integers in [0, 6]."""
    if not weekdays:
        return
    if not isinstance(weekdays, list):
        raise RecurrenceValidationError("weekdays", "Must be a list of integers.")
    for day in weekdays:
        if not isinstance(day, int) or day < 0 or day > 6:
            raise RecurrenceValidationError(
                "weekdays",
                "Each day must be an integer 0 (Monday) to 6 (Sunday).",
            )


def validate_monthly_fields(
    frequency: str,
    monthly_type: str | None,
    day_of_month: int | None,
    nth_weekday: int | None,
    weekday: int | None,
) -> None:
    """Validate monthly recurrence fields given the selected ``monthly_type``."""
    if frequency != _MONTHLY_FREQUENCY:
        return
    if not monthly_type:
        raise RecurrenceValidationError("monthly_type", "Required for monthly recurrence.")
    if monthly_type == _MONTHLY_TYPE_DAY:
        if not day_of_month or day_of_month < 1 or day_of_month > 31:
            raise RecurrenceValidationError("day_of_month", "Must be between 1 and 31.")
    elif monthly_type == _MONTHLY_TYPE_NTH_WEEKDAY:
        if nth_weekday is None or nth_weekday not in (-1, 1, 2, 3, 4):
            raise RecurrenceValidationError("nth_weekday", "Must be 1-4 or -1 (last).")
        if weekday is None or weekday < 0 or weekday > 6:
            raise RecurrenceValidationError("weekday", "Must be 0 (Monday) to 6 (Sunday).")


def validate_boundaries(dtstart: t.Any, until: t.Any, count: t.Any) -> None:
    """Validate mutually-exclusive ``until``/``count`` and ``until > dtstart``."""
    if until and count:
        raise RecurrenceValidationError(
            None,
            "Cannot set both 'until' and 'count'. Choose one or neither.",
        )
    if until and dtstart and until <= dtstart:
        raise RecurrenceValidationError("until", "Must be after dtstart.")


def validate_monthly_field_ranges(
    day_of_month: t.Any,
    nth_weekday: t.Any,
    weekday: t.Any,
) -> None:
    """Validate that monthly fields are in range *if* they are provided.

    Unlike :func:`validate_monthly_fields`, this helper does **not** enforce
    cross-field rules (e.g. "if monthly_type=day then day_of_month is
    required"). It is intended for partial-update payloads where the
    per-field ranges can be checked at the API boundary without needing
    access to the persisted model state. The full cross-field consistency
    check still runs server-side in :meth:`RecurrenceRule.clean`.
    """
    if day_of_month is not None and (day_of_month < 1 or day_of_month > 31):
        raise RecurrenceValidationError("day_of_month", "Must be between 1 and 31.")
    if nth_weekday is not None and nth_weekday not in (-1, 1, 2, 3, 4):
        raise RecurrenceValidationError("nth_weekday", "Must be 1-4 or -1 (last).")
    if weekday is not None and (weekday < 0 or weekday > 6):
        raise RecurrenceValidationError("weekday", "Must be 0 (Monday) to 6 (Sunday).")


def validate_timezone(timezone_name: str | None) -> None:
    """Validate ``timezone_name`` is a real IANA zone name."""
    if not timezone_name:
        raise RecurrenceValidationError("timezone", "Required.")
    try:
        zoneinfo.ZoneInfo(timezone_name)
    except zoneinfo.ZoneInfoNotFoundError as exc:
        raise RecurrenceValidationError(
            "timezone",
            f"Unknown IANA timezone: {timezone_name!r}.",
        ) from exc
