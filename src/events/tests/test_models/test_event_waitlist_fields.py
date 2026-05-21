"""Tests for Event waitlist configuration fields."""

import datetime as dt

import pytest
from django.core.exceptions import ValidationError

from events.models import Event


def _prepare(event: Event) -> Event:
    """Ensure the event has a valid ``end`` so ``full_clean`` focuses on waitlist rules."""
    event.end = event.start + dt.timedelta(hours=2)
    return event


@pytest.mark.django_db
class TestWaitlistConfigDefaults:
    """New waitlist fields keep existing behavior when unset."""

    def test_defaults(self, event: Event) -> None:
        assert event.waitlist_time_window is None
        assert event.waitlist_batch_size == 0
        assert event.waitlist_cutoff_date is None
        assert event.waitlist_lottery_mode is False


@pytest.mark.django_db
class TestWaitlistConfigValidation:
    def test_time_window_min_1h(self, event: Event) -> None:
        _prepare(event)
        event.waitlist_time_window = dt.timedelta(minutes=30)
        with pytest.raises(ValidationError) as exc_info:
            event.full_clean()
        assert "waitlist_time_window" in exc_info.value.message_dict

    def test_time_window_max_7d(self, event: Event) -> None:
        _prepare(event)
        event.waitlist_time_window = dt.timedelta(days=8)
        with pytest.raises(ValidationError) as exc_info:
            event.full_clean()
        assert "waitlist_time_window" in exc_info.value.message_dict

    def test_time_window_inside_bounds(self, event: Event) -> None:
        _prepare(event)
        event.waitlist_time_window = dt.timedelta(hours=24)
        event.full_clean()
        assert event.waitlist_time_window == dt.timedelta(hours=24)

    def test_cutoff_date_must_be_before_start(self, event: Event) -> None:
        _prepare(event)
        event.waitlist_time_window = dt.timedelta(hours=24)
        event.waitlist_cutoff_date = event.start + dt.timedelta(hours=1)
        with pytest.raises(ValidationError) as exc_info:
            event.full_clean()
        assert "waitlist_cutoff_date" in exc_info.value.message_dict

    def test_cutoff_requires_time_window(self, event: Event) -> None:
        _prepare(event)
        event.waitlist_time_window = None
        event.waitlist_cutoff_date = event.start - dt.timedelta(hours=1)
        with pytest.raises(ValidationError) as exc_info:
            event.full_clean()
        assert "waitlist_cutoff_date" in exc_info.value.message_dict

    def test_full_valid_config(self, event: Event) -> None:
        _prepare(event)
        event.waitlist_time_window = dt.timedelta(hours=24)
        event.waitlist_batch_size = 5
        event.waitlist_cutoff_date = event.start - dt.timedelta(hours=2)
        event.waitlist_lottery_mode = True
        event.full_clean()  # must not raise
        assert event.waitlist_lottery_mode is True
        assert event.waitlist_batch_size == 5
