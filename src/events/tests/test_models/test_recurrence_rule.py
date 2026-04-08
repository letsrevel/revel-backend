"""Tests for the RecurrenceRule model."""

from datetime import datetime, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from events.models import RecurrenceRule

pytestmark = pytest.mark.django_db


class TestRecurrenceRuleWeekly:
    """Tests for weekly recurrence rules."""

    def test_weekly_rule_creates_with_correct_weekdays(self) -> None:
        """Test that a weekly rule with specific weekdays is created and rrule_string is populated."""
        # Arrange
        dtstart = timezone.now()

        # Act
        rule = RecurrenceRule.objects.create(
            frequency=RecurrenceRule.Frequency.WEEKLY,
            interval=1,
            weekdays=[0, 2, 4],  # Mon, Wed, Fri
            dtstart=dtstart,
        )

        # Assert
        assert rule.rrule_string != ""
        assert rule.frequency == RecurrenceRule.Frequency.WEEKLY

    def test_weekly_rule_to_rrule_returns_correct_occurrences(self) -> None:
        """Test that to_rrule() produces occurrences on the specified weekdays."""
        # Arrange - use a known Monday as dtstart
        dtstart = timezone.make_aware(datetime(2026, 4, 6, 10, 0))  # Monday
        rule = RecurrenceRule(
            frequency=RecurrenceRule.Frequency.WEEKLY,
            interval=1,
            weekdays=[0, 2, 4],  # Mon, Wed, Fri
            dtstart=dtstart,
            count=6,
        )

        # Act
        rrule_obj = rule.to_rrule()
        occurrences = list(rrule_obj)

        # Assert
        assert len(occurrences) == 6
        # All occurrences should be Mon (0), Wed (2), or Fri (4)
        for occ in occurrences:
            assert occ.weekday() in [0, 2, 4]

    def test_weekly_rule_with_interval(self) -> None:
        """Test that a weekly rule with interval=2 generates every other week."""
        # Arrange
        dtstart = timezone.make_aware(datetime(2026, 4, 6, 10, 0))  # Monday
        rule = RecurrenceRule(
            frequency=RecurrenceRule.Frequency.WEEKLY,
            interval=2,
            weekdays=[0],  # Monday only
            dtstart=dtstart,
            count=3,
        )

        # Act
        rrule_obj = rule.to_rrule()
        occurrences = list(rrule_obj)

        # Assert
        assert len(occurrences) == 3
        # Each occurrence should be 2 weeks apart
        assert (occurrences[1] - occurrences[0]).days == 14
        assert (occurrences[2] - occurrences[1]).days == 14


class TestRecurrenceRuleMonthly:
    """Tests for monthly recurrence rules."""

    def test_monthly_day_of_month_rule(self) -> None:
        """Test that a monthly rule with DAY_OF_MONTH generates on the 15th."""
        # Arrange
        dtstart = timezone.make_aware(datetime(2026, 1, 15, 10, 0))
        rule = RecurrenceRule(
            frequency=RecurrenceRule.Frequency.MONTHLY,
            monthly_type=RecurrenceRule.MonthlyType.DAY_OF_MONTH,
            day_of_month=15,
            dtstart=dtstart,
            count=4,
        )

        # Act
        rrule_obj = rule.to_rrule()
        occurrences = list(rrule_obj)

        # Assert
        assert len(occurrences) == 4
        for occ in occurrences:
            assert occ.day == 15

    def test_monthly_nth_weekday_rule(self) -> None:
        """Test that a monthly rule with NTH_WEEKDAY=2, weekday=1 generates on the 2nd Tuesday."""
        # Arrange
        dtstart = timezone.make_aware(datetime(2026, 1, 1, 10, 0))
        rule = RecurrenceRule(
            frequency=RecurrenceRule.Frequency.MONTHLY,
            monthly_type=RecurrenceRule.MonthlyType.NTH_WEEKDAY,
            nth_weekday=2,  # 2nd
            weekday=1,  # Tuesday
            dtstart=dtstart,
            count=4,
        )

        # Act
        rrule_obj = rule.to_rrule()
        occurrences = list(rrule_obj)

        # Assert
        assert len(occurrences) == 4
        for occ in occurrences:
            assert occ.weekday() == 1  # Tuesday
            # 2nd occurrence of the weekday = day falls between 8 and 14
            assert 8 <= occ.day <= 14

    def test_monthly_last_weekday_rule(self) -> None:
        """Test that a monthly rule with NTH_WEEKDAY=-1 generates the last occurrence."""
        # Arrange
        dtstart = timezone.make_aware(datetime(2026, 1, 1, 10, 0))
        rule = RecurrenceRule(
            frequency=RecurrenceRule.Frequency.MONTHLY,
            monthly_type=RecurrenceRule.MonthlyType.NTH_WEEKDAY,
            nth_weekday=-1,  # last
            weekday=4,  # Friday
            dtstart=dtstart,
            count=3,
        )

        # Act
        rrule_obj = rule.to_rrule()
        occurrences = list(rrule_obj)

        # Assert — each occurrence must be the LAST Friday of its month, not
        # just any Friday. Adding 7 days should cross into the next month.
        assert len(occurrences) == 3
        for occ in occurrences:
            assert occ.weekday() == 4  # Friday
            next_week = occ + timedelta(days=7)
            assert next_week.month != occ.month, f"Expected last Friday of the month, but {occ.date()} is not"


class TestRecurrenceRuleDaily:
    """Tests for daily recurrence rules."""

    def test_daily_rule_with_interval(self) -> None:
        """Test that a daily rule with interval=2 generates every other day."""
        # Arrange
        dtstart = timezone.make_aware(datetime(2026, 4, 1, 10, 0))
        rule = RecurrenceRule(
            frequency=RecurrenceRule.Frequency.DAILY,
            interval=2,
            dtstart=dtstart,
            count=5,
        )

        # Act
        rrule_obj = rule.to_rrule()
        occurrences = list(rrule_obj)

        # Assert
        assert len(occurrences) == 5
        for i in range(1, len(occurrences)):
            assert (occurrences[i] - occurrences[i - 1]).days == 2


class TestRecurrenceRuleValidation:
    """Tests for RecurrenceRule clean() validation logic."""

    def test_weekdays_out_of_range_raises_validation_error(self) -> None:
        """Test that weekdays containing 7 raises a ValidationError."""
        # Arrange
        rule = RecurrenceRule(
            frequency=RecurrenceRule.Frequency.WEEKLY,
            weekdays=[0, 7],  # 7 is out of range
            dtstart=timezone.now(),
        )

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            rule.clean()
        assert "weekdays" in exc_info.value.message_dict

    def test_weekdays_negative_raises_validation_error(self) -> None:
        """Test that a negative weekday value raises a ValidationError."""
        # Arrange
        rule = RecurrenceRule(
            frequency=RecurrenceRule.Frequency.WEEKLY,
            weekdays=[-1],
            dtstart=timezone.now(),
        )

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            rule.clean()
        assert "weekdays" in exc_info.value.message_dict

    def test_weekdays_non_integer_raises_validation_error(self) -> None:
        """Test that non-integer weekday values raise a ValidationError."""
        # Arrange
        rule = RecurrenceRule(
            frequency=RecurrenceRule.Frequency.WEEKLY,
            weekdays=["monday"],
            dtstart=timezone.now(),
        )

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            rule.clean()
        assert "weekdays" in exc_info.value.message_dict

    def test_until_before_dtstart_raises_validation_error(self) -> None:
        """Test that until < dtstart raises a ValidationError."""
        # Arrange
        now = timezone.now()
        rule = RecurrenceRule(
            frequency=RecurrenceRule.Frequency.DAILY,
            dtstart=now,
            until=now - timedelta(days=1),
        )

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            rule.clean()
        assert "until" in exc_info.value.message_dict

    def test_until_equal_to_dtstart_raises_validation_error(self) -> None:
        """Test that until == dtstart raises a ValidationError (must be strictly after)."""
        # Arrange
        now = timezone.now()
        rule = RecurrenceRule(
            frequency=RecurrenceRule.Frequency.DAILY,
            dtstart=now,
            until=now,
        )

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            rule.clean()
        assert "until" in exc_info.value.message_dict

    def test_both_until_and_count_raises_validation_error(self) -> None:
        """Test that setting both until and count raises a ValidationError."""
        # Arrange
        now = timezone.now()
        rule = RecurrenceRule(
            frequency=RecurrenceRule.Frequency.DAILY,
            dtstart=now,
            until=now + timedelta(days=30),
            count=10,
        )

        # Act & Assert
        with pytest.raises(ValidationError):
            rule.clean()

    def test_monthly_without_monthly_type_raises_validation_error(self) -> None:
        """Test that a monthly rule without monthly_type raises a ValidationError."""
        # Arrange
        rule = RecurrenceRule(
            frequency=RecurrenceRule.Frequency.MONTHLY,
            dtstart=timezone.now(),
        )

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            rule.clean()
        assert "monthly_type" in exc_info.value.message_dict

    def test_monthly_day_of_month_invalid_day_raises_validation_error(self) -> None:
        """Test that day_of_month=0 raises a ValidationError for DAY_OF_MONTH monthly type."""
        # Arrange
        rule = RecurrenceRule(
            frequency=RecurrenceRule.Frequency.MONTHLY,
            monthly_type=RecurrenceRule.MonthlyType.DAY_OF_MONTH,
            day_of_month=0,
            dtstart=timezone.now(),
        )

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            rule.clean()
        assert "day_of_month" in exc_info.value.message_dict

    def test_monthly_day_of_month_too_high_raises_validation_error(self) -> None:
        """Test that day_of_month=32 raises a ValidationError."""
        # Arrange
        rule = RecurrenceRule(
            frequency=RecurrenceRule.Frequency.MONTHLY,
            monthly_type=RecurrenceRule.MonthlyType.DAY_OF_MONTH,
            day_of_month=32,
            dtstart=timezone.now(),
        )

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            rule.clean()
        assert "day_of_month" in exc_info.value.message_dict

    def test_monthly_nth_weekday_invalid_nth_raises_validation_error(self) -> None:
        """Test that nth_weekday=5 raises a ValidationError (valid: 1-4 or -1)."""
        # Arrange
        rule = RecurrenceRule(
            frequency=RecurrenceRule.Frequency.MONTHLY,
            monthly_type=RecurrenceRule.MonthlyType.NTH_WEEKDAY,
            nth_weekday=5,
            weekday=1,
            dtstart=timezone.now(),
        )

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            rule.clean()
        assert "nth_weekday" in exc_info.value.message_dict

    def test_monthly_nth_weekday_missing_weekday_raises_validation_error(self) -> None:
        """Test that NTH_WEEKDAY without weekday raises a ValidationError."""
        # Arrange
        rule = RecurrenceRule(
            frequency=RecurrenceRule.Frequency.MONTHLY,
            monthly_type=RecurrenceRule.MonthlyType.NTH_WEEKDAY,
            nth_weekday=2,
            weekday=None,
            dtstart=timezone.now(),
        )

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            rule.clean()
        assert "weekday" in exc_info.value.message_dict

    def test_valid_rule_passes_clean(self) -> None:
        """Test that a properly configured rule passes validation without errors."""
        # Arrange
        rule = RecurrenceRule(
            frequency=RecurrenceRule.Frequency.WEEKLY,
            weekdays=[0, 2, 4],
            dtstart=timezone.now(),
            count=10,
        )

        # Act & Assert - should not raise
        rule.clean()

    def test_invalid_timezone_raises_validation_error(self) -> None:
        """Test that a non-IANA timezone string is rejected at clean() time."""
        # Arrange
        rule = RecurrenceRule(
            frequency=RecurrenceRule.Frequency.DAILY,
            dtstart=timezone.now(),
            timezone="Mars/Olympus_Mons",
        )

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            rule.clean()
        assert "timezone" in exc_info.value.message_dict

    def test_empty_timezone_raises_validation_error(self) -> None:
        """Test that an empty timezone string is rejected."""
        # Arrange
        rule = RecurrenceRule(
            frequency=RecurrenceRule.Frequency.DAILY,
            dtstart=timezone.now(),
            timezone="",
        )

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            rule.clean()
        assert "timezone" in exc_info.value.message_dict

    def test_valid_iana_timezone_passes(self) -> None:
        """Test that a valid IANA timezone passes validation."""
        # Arrange
        rule = RecurrenceRule(
            frequency=RecurrenceRule.Frequency.DAILY,
            dtstart=timezone.now(),
            timezone="Europe/Rome",
        )

        # Act & Assert — must not raise
        rule.clean()


class TestRecurrenceRuleGetOccurrences:
    """Tests for the get_occurrences method."""

    def test_get_occurrences_returns_dates_in_range(self) -> None:
        """Test that get_occurrences returns only dates within the given range."""
        # Arrange
        dtstart = timezone.make_aware(datetime(2026, 4, 1, 10, 0))
        rule = RecurrenceRule(
            frequency=RecurrenceRule.Frequency.DAILY,
            interval=1,
            dtstart=dtstart,
        )

        # Occurrences at 10:00 each day. between() is exclusive on both ends.
        # Apr 3 10:00 > Apr 3 00:00 => included
        # Apr 4 10:00, Apr 5 10:00, Apr 6 10:00 => included
        # So we expect Apr 3, 4, 5, 6 at 10:00 each => 4 occurrences
        after = timezone.make_aware(datetime(2026, 4, 3, 0, 0))
        before = timezone.make_aware(datetime(2026, 4, 7, 0, 0))

        # Act
        occurrences = rule.get_occurrences(after=after, before=before)

        # Assert
        assert len(occurrences) == 4
        for occ in occurrences:
            assert occ > after
            assert occ < before

    def test_get_occurrences_empty_when_no_dates_in_range(self) -> None:
        """Test that get_occurrences returns empty list when no dates fall in range."""
        # Arrange
        dtstart = timezone.make_aware(datetime(2026, 4, 1, 10, 0))
        rule = RecurrenceRule(
            frequency=RecurrenceRule.Frequency.WEEKLY,
            interval=1,
            weekdays=[0],  # Monday only
            dtstart=dtstart,
            count=2,
        )

        # Ask for a range that has no Mondays
        after = timezone.make_aware(datetime(2026, 4, 7, 11, 0))  # After second Monday
        before = timezone.make_aware(datetime(2026, 4, 10, 0, 0))  # Before next Monday

        # Act
        occurrences = rule.get_occurrences(after=after, before=before)

        # Assert
        assert occurrences == []


class TestRecurrenceRuleSave:
    """Tests for rrule_string computation on save."""

    def test_rrule_string_populated_on_save(self) -> None:
        """Test that rrule_string is computed when the model is saved."""
        # Arrange
        rule = RecurrenceRule(
            frequency=RecurrenceRule.Frequency.DAILY,
            interval=1,
            dtstart=timezone.now(),
            count=5,
        )
        assert rule.rrule_string == ""

        # Act
        rule.save()

        # Assert
        assert rule.rrule_string != ""
        assert "FREQ" in rule.rrule_string

    def test_rrule_string_recomputed_on_field_change(self) -> None:
        """Test that changing a field and saving recomputes the rrule_string."""
        # Arrange
        rule = RecurrenceRule.objects.create(
            frequency=RecurrenceRule.Frequency.DAILY,
            interval=1,
            dtstart=timezone.now(),
            count=5,
        )
        original_string = rule.rrule_string

        # Act
        rule.interval = 3
        rule.save()

        # Assert
        assert rule.rrule_string != original_string

    def test_rrule_string_persisted_with_update_fields(self) -> None:
        """A save that passes ``update_fields`` without ``rrule_string`` must
        still persist the recomputed RRULE — the model auto-adds it to the
        update_fields set, preventing a stale RRULE on disk."""
        # Arrange
        rule = RecurrenceRule.objects.create(
            frequency=RecurrenceRule.Frequency.DAILY,
            interval=1,
            dtstart=timezone.now(),
            count=5,
        )
        original_string = rule.rrule_string
        assert "FREQ=DAILY" in original_string

        # Act — change frequency and save with update_fields that OMITS rrule_string.
        rule.frequency = RecurrenceRule.Frequency.WEEKLY
        rule.save(update_fields=["frequency"])

        # Assert — reload from DB and verify rrule_string was persisted.
        rule.refresh_from_db()
        assert "FREQ=WEEKLY" in rule.rrule_string
        assert rule.rrule_string != original_string

    def test_str_representation(self) -> None:
        """Test the string representation of a RecurrenceRule."""
        # Arrange
        rule = RecurrenceRule(
            frequency=RecurrenceRule.Frequency.WEEKLY,
            interval=2,
            dtstart=timezone.now(),
        )

        # Act
        result = str(rule)

        # Assert
        assert "Weekly" in result
        assert "2" in result


class TestRecurrenceRuleDSTBehavior:
    """Lock in the (current, pre-Phase-3) DST behavior of recurrence rules.

    The ``timezone`` field is currently advisory metadata only — occurrences
    are anchored to the UTC ``dtstart`` and do **not** observe DST in the
    named zone. This is documented in the model's field comment and in the
    ``RecurrenceRuleCreateSchema.timezone`` description. These tests pin the
    behavior so the eventual Phase 3 fix is intentional, observable, and
    breaks something visible (forcing an update to these tests in lockstep
    with the fix).
    """

    def test_weekly_rule_anchors_to_utc_across_dst_transition(self) -> None:
        """Weekly rule starting before DST yields the same UTC time after DST.

        This is the bug that Phase 3 will fix. A user expecting "Mondays at
        10:00 in their local time" gets "Mondays at the UTC instant they
        originally picked," which drifts by 1 hour relative to wall-clock
        time after DST.
        """
        import zoneinfo
        from datetime import timezone as dt_timezone

        # Arrange — Monday 2026-03-23 10:00 Europe/Vienna (CET, UTC+1).
        # The spring DST transition happens on 2026-03-29, so the next
        # Monday (2026-03-30) is in CEST (UTC+2). A timezone-aware system
        # would yield 10:00 Vienna both weeks; the current behavior yields
        # 09:00 UTC both weeks, which is 11:00 Vienna in the second week.
        vienna = zoneinfo.ZoneInfo("Europe/Vienna")
        dtstart_local = datetime(2026, 3, 23, 10, 0, tzinfo=vienna)

        rule = RecurrenceRule(
            frequency=RecurrenceRule.Frequency.WEEKLY,
            interval=1,
            weekdays=[0],  # Monday
            dtstart=dtstart_local,
            count=2,
            timezone="Europe/Vienna",
        )

        # Act
        occurrences = list(rule.to_rrule())

        # Assert — both occurrences share the same UTC instant offset from
        # midnight, even though the second one falls after DST. A
        # timezone-aware Phase 3 implementation would shift the second
        # occurrence by one hour to keep the wall-clock time stable.
        assert len(occurrences) == 2
        first_utc = occurrences[0].astimezone(dt_timezone.utc)
        second_utc = occurrences[1].astimezone(dt_timezone.utc)
        assert first_utc.hour == second_utc.hour, (
            "Phase 1/2 anchors occurrences to UTC dtstart and ignores DST. "
            "If this assertion starts failing, Phase 3 has been implemented "
            "and the field comment + schema description should be updated."
        )
        # And the wall-clock time in Vienna drifts by exactly one hour.
        first_vienna = occurrences[0].astimezone(vienna)
        second_vienna = occurrences[1].astimezone(vienna)
        assert second_vienna.hour - first_vienna.hour == 1
