# ruff: noqa: W293

from datetime import datetime
from unittest.mock import MagicMock, Mock, patch
from zoneinfo import ZoneInfo

import pytest
from django.contrib.gis.geos import Point

from accounts.models import RevelUser
from events import models
from events.utils import (
    create_ticket_pdf,
    format_event_datetime,
    get_event_timezone,
    get_invitation_message,
)
from geo.models import City


@pytest.mark.django_db
def test_get_invitation_message_default_template(public_user: RevelUser, public_event: models.Event) -> None:
    """Test that the default invitation message template is used when event has no custom message."""
    public_event.invitation_message = ""
    public_event.save()

    display_name = public_user.get_display_name()
    message = get_invitation_message(display_name, public_event)

    assert f"Hello {display_name}!" in message
    assert f"You have been invited to {public_event.name}!" in message


@pytest.mark.django_db
def test_get_invitation_message_custom_template(public_user: RevelUser, public_event: models.Event) -> None:
    """Test that a custom invitation message is used when provided by the event."""
    custom_message = "Hi {user_name}! Welcome to {event_name}. This is a custom message."
    public_event.invitation_message = custom_message
    public_event.save()

    display_name = public_user.get_display_name()
    message = get_invitation_message(display_name, public_event)

    expected = f"Hi {display_name}! Welcome to {public_event.name}. This is a custom message."
    assert message == expected


@pytest.mark.django_db
def test_get_invitation_message_with_event_description(public_user: RevelUser, public_event: models.Event) -> None:
    """Test that the event description is included in the default template when available."""
    public_event.invitation_message = ""
    public_event.description = "This is a test event description."
    public_event.save()

    message = get_invitation_message(public_user.get_display_name(), public_event)

    assert public_event.description in message


@pytest.mark.django_db
def test_get_invitation_message_with_template_variables(public_user: RevelUser, public_event: models.Event) -> None:
    """Test that safe {placeholder} variables are correctly substituted in custom invitation messages."""
    custom_message = (
        "Hello {user_name}! You're invited to {event_name} on {event_date}. Organized by: {organization_name}."
    )
    public_event.invitation_message = custom_message
    public_event.save()

    display_name = public_user.get_display_name()
    message = get_invitation_message(display_name, public_event)

    assert display_name in message
    assert public_event.name in message
    assert public_event.organization.name in message


@pytest.mark.django_db
def test_get_invitation_message_unknown_placeholder_resolves_to_empty(
    public_user: RevelUser, public_event: models.Event
) -> None:
    """Unknown placeholders are silently replaced with empty string (no KeyError, no data leak)."""
    public_event.invitation_message = "Hi {user_name}! Secret: {user.email}."
    public_event.save()

    display_name = public_user.get_display_name()
    message = get_invitation_message(display_name, public_event)

    assert display_name in message
    # The unknown placeholder resolves to empty string, not a real email
    assert public_user.email not in message
    assert "{user.email}" not in message


@pytest.mark.django_db
def test_get_invitation_message_django_template_syntax_not_executed(
    public_user: RevelUser, public_event: models.Event
) -> None:
    """Legacy Django template syntax is treated as literal text, preventing SSTI."""
    public_event.invitation_message = "Hello {{ user.email }}!"
    public_event.save()

    message = get_invitation_message(public_user.get_display_name(), public_event)

    # Django template syntax must NOT be executed — email must not appear
    assert public_user.email not in message


@pytest.mark.django_db
def test_get_invitation_message_malformed_format_returns_raw(
    public_user: RevelUser, public_event: models.Event
) -> None:
    """Malformed format strings (stray braces) fall back to the raw invitation message without raising."""
    public_event.invitation_message = "Save 50% }"  # stray '}' causes ValueError in format_map
    public_event.save()
    message = get_invitation_message(public_user.get_display_name(), public_event)
    assert message == "Save 50% }"


@pytest.mark.django_db
def test_get_invitation_message_with_email_as_display_name(public_event: models.Event) -> None:
    """Test that an email address can be used as display name for pending invitations."""
    public_event.invitation_message = "Hi {user_name}! You're invited to {event_name}."
    public_event.save()

    email = "newuser@example.com"
    message = get_invitation_message(email, public_event)

    assert f"Hi {email}!" in message
    assert public_event.name in message


@pytest.mark.django_db
@patch("events.utils.qrcode.QRCode")
@patch("events.utils.HTML")
@patch("events.utils.render_to_string")
def test_create_ticket_pdf_basic_functionality(
    mock_render: Mock, mock_html: Mock, mock_qr: Mock, ticket: models.Ticket
) -> None:
    """Test that create_ticket_pdf generates a PDF with correct data."""
    # Mock the QR code generation
    mock_qr_instance = Mock()
    mock_qr.return_value = mock_qr_instance
    mock_img = Mock()
    mock_qr_instance.make_image.return_value = mock_img

    # Mock HTML rendering
    mock_html_instance = Mock()
    mock_html.return_value = mock_html_instance
    mock_html_instance.write_pdf.return_value = b"fake-pdf-content"

    # Mock template rendering
    mock_render.return_value = "<html><body>Ticket</body></html>"

    # Call the function
    pdf_bytes = create_ticket_pdf(ticket)

    # Assert QR code was created with ticket ID
    mock_qr.assert_called_once()
    mock_qr_instance.add_data.assert_called_once_with(str(ticket.id))
    mock_qr_instance.make.assert_called_once_with(fit=True)

    # Assert template was rendered with correct context
    mock_render.assert_called_once()
    args, kwargs = mock_render.call_args
    assert args[0] == "events/ticket.html"
    context = kwargs["context"]
    assert "event_name" in context
    assert "organization_name" in context
    assert "user_display_name" in context
    assert "tier_name" in context
    assert "qr_code_base64" in context
    assert "ticket_id" in context

    # Assert HTML was converted to PDF
    mock_html.assert_called_once_with(string="<html><body>Ticket</body></html>")
    mock_html_instance.write_pdf.assert_called_once()

    # Assert correct return value
    assert pdf_bytes == b"fake-pdf-content"


@pytest.mark.django_db
@patch("events.utils.qrcode.QRCode")
@patch("events.utils.HTML")
@patch("events.utils.render_to_string")
def test_create_ticket_pdf_context_data(
    mock_render: Mock, mock_html: Mock, mock_qr: Mock, ticket: models.Ticket
) -> None:
    """Test that create_ticket_pdf passes correct context data to template."""
    # Mock dependencies
    mock_qr_instance = Mock()
    mock_qr.return_value = mock_qr_instance
    mock_img = Mock()
    mock_qr_instance.make_image.return_value = mock_img

    mock_html_instance = Mock()
    mock_html.return_value = mock_html_instance
    mock_html_instance.write_pdf.return_value = b"fake-pdf"

    mock_render.return_value = "<html></html>"

    # Call the function
    create_ticket_pdf(ticket)

    # Check context data passed to template
    _, kwargs = mock_render.call_args
    context = kwargs["context"]

    assert context["event_name"] == ticket.event.name
    assert context["organization_name"] == ticket.event.organization.name
    assert context["user_display_name"] == ticket.user.get_display_name()
    assert context["tier_name"] == ticket.tier.name
    assert context["ticket_id"] == str(ticket.id)
    assert "qr_code_base64" in context
    assert "start_datetime" in context


@pytest.mark.django_db
@patch("events.utils.qrcode.QRCode")
@patch("events.utils.HTML")
@patch("events.utils.render_to_string")
def test_create_ticket_pdf_handles_missing_address(
    mock_render: Mock, mock_html: Mock, mock_qr: Mock, ticket: models.Ticket
) -> None:
    """Test that create_ticket_pdf handles missing address gracefully."""
    # Ensure event has no address
    ticket.event.address = None
    ticket.event.city = None
    ticket.event.save()

    # Mock dependencies
    mock_qr_instance = Mock()
    mock_qr.return_value = mock_qr_instance
    mock_img = Mock()
    mock_qr_instance.make_image.return_value = mock_img

    mock_html_instance = Mock()
    mock_html.return_value = mock_html_instance
    mock_html_instance.write_pdf.return_value = b"fake-pdf"

    mock_render.return_value = "<html></html>"

    # Call the function
    create_ticket_pdf(ticket)

    # Check that address is empty string when neither address nor city exist
    _, kwargs = mock_render.call_args
    context = kwargs["context"]
    assert context["address"] == ""


# --- Timezone formatting tests ---


@pytest.fixture
def vienna_city(db: None) -> City:
    """Create a Vienna city with timezone."""
    return City.objects.create(
        name="Vienna",
        ascii_name="Vienna",
        country="Austria",
        iso2="AT",
        iso3="AUT",
        city_id=99999,
        location=Point(16.3738, 48.2082, srid=4326),
        timezone="Europe/Vienna",
    )


@pytest.fixture
def new_york_city(db: None) -> City:
    """Create a New York city with timezone."""
    return City.objects.create(
        name="New York",
        ascii_name="New York",
        country="United States",
        iso2="US",
        iso3="USA",
        city_id=99998,
        location=Point(-74.0060, 40.7128, srid=4326),
        timezone="America/New_York",
    )


def _mock_event(city: City | None) -> MagicMock:
    """Create a mock event with the given city."""
    event = MagicMock()
    event.city = city
    return event


class TestGetEventTimezone:
    """Tests for get_event_timezone function."""

    def test_returns_city_timezone(self, vienna_city: City) -> None:
        event = _mock_event(vienna_city)
        assert get_event_timezone(event) == ZoneInfo("Europe/Vienna")

    def test_returns_utc_when_no_city(self) -> None:
        event = _mock_event(None)
        assert get_event_timezone(event) == ZoneInfo("UTC")

    def test_returns_utc_when_city_has_no_timezone(self) -> None:
        city = MagicMock()
        city.timezone = None
        event = _mock_event(city)
        assert get_event_timezone(event) == ZoneInfo("UTC")

    def test_returns_utc_for_invalid_timezone(self) -> None:
        city = MagicMock()
        city.id = 1
        city.timezone = "Invalid/Timezone"
        event = _mock_event(city)
        assert get_event_timezone(event) == ZoneInfo("UTC")


class TestFormatEventDatetime:
    """Tests for format_event_datetime function."""

    def test_converts_to_event_timezone(self, vienna_city: City) -> None:
        """18:00 UTC should become 19:00 CET in Vienna (winter)."""
        event = _mock_event(vienna_city)
        dt = datetime(2026, 2, 6, 18, 0, 0, tzinfo=ZoneInfo("UTC"))

        result = format_event_datetime(dt, event)

        assert "7:00 PM" in result
        assert "CET" in result

    def test_returns_empty_for_none(self, vienna_city: City) -> None:
        event = _mock_event(vienna_city)
        assert format_event_datetime(None, event) == ""

    def test_utc_fallback_when_no_city(self) -> None:
        event = _mock_event(None)
        dt = datetime(2026, 2, 6, 18, 0, 0, tzinfo=ZoneInfo("UTC"))

        result = format_event_datetime(dt, event)

        assert "6:00 PM" in result
        assert "UTC" in result

    def test_different_timezones_differ(self, vienna_city: City, new_york_city: City) -> None:
        dt = datetime(2026, 2, 6, 18, 0, 0, tzinfo=ZoneInfo("UTC"))

        result_vienna = format_event_datetime(dt, _mock_event(vienna_city))
        result_ny = format_event_datetime(dt, _mock_event(new_york_city))

        assert result_vienna != result_ny
        # Vienna: 19:00 CET, New York: 13:00 EST
        assert "7:00 PM" in result_vienna
        assert "1:00 PM" in result_ny

    def test_custom_format(self, vienna_city: City) -> None:
        event = _mock_event(vienna_city)
        dt = datetime(2026, 2, 6, 18, 0, 0, tzinfo=ZoneInfo("UTC"))

        result = format_event_datetime(dt, event, fmt="Y-m-d H:i")

        assert "2026-02-06 19:00" == result


@pytest.mark.django_db
@patch("events.utils.qrcode.QRCode")
@patch("events.utils.HTML")
@patch("events.utils.render_to_string")
def test_create_ticket_pdf_uses_event_timezone(
    mock_render: Mock, mock_html: Mock, mock_qr: Mock, ticket: models.Ticket, vienna_city: City
) -> None:
    """Test that PDF ticket start_datetime is formatted in the event's timezone."""
    # Set up the event with Vienna timezone
    ticket.event.city = vienna_city
    ticket.event.start = datetime(2026, 2, 6, 18, 0, 0, tzinfo=ZoneInfo("UTC"))
    ticket.event.save()

    # Mock dependencies
    mock_qr_instance = Mock()
    mock_qr.return_value = mock_qr_instance
    mock_qr_instance.make_image.return_value = Mock()
    mock_html_instance = Mock()
    mock_html.return_value = mock_html_instance
    mock_html_instance.write_pdf.return_value = b"fake-pdf"
    mock_render.return_value = "<html></html>"

    create_ticket_pdf(ticket)

    _, kwargs = mock_render.call_args
    start_dt = kwargs["context"]["start_datetime"]
    # 18:00 UTC = 19:00 CET
    assert "7:00 PM" in start_dt
    assert "CET" in start_dt
