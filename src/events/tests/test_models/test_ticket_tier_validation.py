from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from events.models import Event, TicketTier

pytestmark = pytest.mark.django_db


class TestTicketTierValidation:
    """Test TicketTier clean validation method."""

    def test_valid_sales_window_passes_validation(self, public_event: Event) -> None:
        """Test that a valid sales window passes validation."""
        tier = TicketTier(
            event=public_event,
            name="Early Bird",
            sales_start_at=timezone.now() + timedelta(hours=1),
            sales_end_at=timezone.now() + timedelta(hours=12),
        )
        # Should not raise ValidationError
        tier.clean()

    def test_sales_start_after_event_start_fails_validation(self, public_event: Event) -> None:
        """Test that sales_start_at after event start fails validation."""
        public_event.start = timezone.now() - timedelta(hours=1)
        tier = TicketTier(
            event=public_event,
            name="Invalid Start",
            sales_start_at=timezone.now() + timedelta(hours=25),  # After event start
            sales_end_at=timezone.now() + timedelta(hours=26),
        )
        with pytest.raises(ValidationError) as exc_info:
            tier.clean()

        assert "sales_start_at" in exc_info.value.message_dict
        assert "before or at the event start time" in exc_info.value.message_dict["sales_start_at"][0]

    def test_sales_end_before_sales_start_fails_validation(self, public_event: Event) -> None:
        """Test that sales_end_at before sales_start_at fails validation."""
        tier = TicketTier(
            event=public_event,
            name="Invalid End",
            sales_start_at=timezone.now() + timedelta(hours=12),
            sales_end_at=timezone.now() + timedelta(hours=6),  # Before sales start
        )
        with pytest.raises(ValidationError) as exc_info:
            tier.clean()

        assert "sales_end_at" in exc_info.value.message_dict
        assert "after the sales start time" in exc_info.value.message_dict["sales_end_at"][0]

    def test_sales_end_equal_sales_start_fails_validation(self, public_event: Event) -> None:
        """Test that sales_end_at equal to sales_start_at fails validation."""
        same_time = timezone.now() + timedelta(hours=12)
        tier = TicketTier(
            event=public_event,
            name="Equal Times",
            sales_start_at=same_time,
            sales_end_at=same_time,
        )
        with pytest.raises(ValidationError) as exc_info:
            tier.clean()

        assert "sales_end_at" in exc_info.value.message_dict
        assert "after the sales start time" in exc_info.value.message_dict["sales_end_at"][0]

    def test_none_sales_times_pass_validation(self, public_event: Event) -> None:
        """Test that None sales times pass validation."""
        tier = TicketTier(
            event=public_event,
            name="No Times",
            sales_start_at=None,
            sales_end_at=None,
        )
        # Should not raise ValidationError
        tier.clean()

    def test_only_sales_start_passes_validation(self, public_event: Event) -> None:
        """Test that only having sales_start_at passes validation."""
        tier = TicketTier(
            event=public_event,
            name="Only Start",
            sales_start_at=timezone.now() + timedelta(hours=1),
            sales_end_at=None,
        )
        # Should not raise ValidationError
        tier.clean()

    def test_only_sales_end_passes_validation(self, public_event: Event) -> None:
        """Test that only having sales_end_at passes validation."""
        tier = TicketTier(
            event=public_event,
            name="Only End",
            sales_start_at=None,
            sales_end_at=timezone.now() + timedelta(hours=12),
        )
        # Should not raise ValidationError
        tier.clean()

    def test_sales_start_equal_event_start_passes_validation(self, public_event: Event) -> None:
        """Test that sales_start_at equal to event start passes validation."""
        tier = TicketTier(
            event=public_event,
            name="Exact Start",
            sales_start_at=public_event.start - timedelta(days=7),
            sales_end_at=public_event.start,
        )
        # Should not raise ValidationError
        tier.clean()
