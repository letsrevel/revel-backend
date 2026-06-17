"""Unit tests for the event schedule Pydantic validation."""

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from pydantic import ValidationError

from events.models import Event
from events.utils.schedule import EventScheduleSession, validate_schedule


class TestEventScheduleSession:
    def test_minimal_valid_session(self) -> None:
        session = EventScheduleSession(title="Arrival", offset_minutes=0)
        assert session.title == "Arrival"
        assert session.offset_minutes == 0
        assert session.duration_minutes is None
        assert session.description is None
        assert session.location is None
        assert session.is_required is False

    def test_full_valid_session(self) -> None:
        session = EventScheduleSession(
            title="Consent Workshop",
            description="Please attend.",
            offset_minutes=60,
            duration_minutes=90,
            location="Main Hall",
            is_required=True,
        )
        assert session.duration_minutes == 90
        assert session.is_required is True

    def test_offset_cannot_be_negative(self) -> None:
        with pytest.raises(ValidationError):
            EventScheduleSession(title="X", offset_minutes=-1)

    def test_duration_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            EventScheduleSession(title="X", offset_minutes=0, duration_minutes=0)

    def test_title_required_nonempty(self) -> None:
        with pytest.raises(ValidationError):
            EventScheduleSession(title="", offset_minutes=0)

    def test_description_is_bleached(self) -> None:
        session = EventScheduleSession(
            title="X",
            offset_minutes=0,
            description="<script>alert(1)</script>**ok**",
        )
        assert "<script>" not in (session.description or "")
        assert "ok" in (session.description or "")

    def test_title_not_bleached(self) -> None:
        # title is plain text (frontend auto-escapes); we do not mutate it.
        session = EventScheduleSession(title="A < B", offset_minutes=0)
        assert session.title == "A < B"

    def test_description_strips_dangerous_attributes(self) -> None:
        session = EventScheduleSession(
            title="X",
            offset_minutes=0,
            description='<p onclick="alert(1)">hello</p>',
        )
        assert "onclick" not in (session.description or "")
        assert "hello" in (session.description or "")


class TestValidateSchedule:
    def test_none_returns_empty_list(self) -> None:
        assert validate_schedule(None) == []

    def test_empty_list(self) -> None:
        assert validate_schedule([]) == []

    def test_parses_list_of_dicts(self) -> None:
        result = validate_schedule([{"title": "Arrival", "offset_minutes": 0}])
        assert len(result) == 1
        assert result[0].title == "Arrival"

    def test_rejects_more_than_200_sessions(self) -> None:
        data = [{"title": f"S{i}", "offset_minutes": i} for i in range(201)]
        with pytest.raises(ValidationError):
            validate_schedule(data)

    def test_rejects_malformed_session(self) -> None:
        with pytest.raises(ValidationError):
            validate_schedule([{"offset_minutes": 0}])  # missing title


@pytest.mark.django_db
class TestEventScheduleField:
    def test_default_is_empty_list(self, event: Event) -> None:
        event.refresh_from_db()
        assert event.schedule == []

    def test_clean_accepts_valid_schedule(self, event: Event) -> None:
        event.schedule = [{"title": "Arrival", "offset_minutes": 0}]
        event.full_clean()  # must not raise

    def test_clean_rejects_malformed_schedule(self, event: Event) -> None:
        event.schedule = [{"offset_minutes": 0}]  # missing title
        with pytest.raises(DjangoValidationError) as exc_info:
            event.full_clean()
        assert "schedule" in exc_info.value.message_dict
