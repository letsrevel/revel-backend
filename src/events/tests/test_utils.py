# ruff: noqa: W293

from unittest.mock import Mock, patch

import pytest

from accounts.models import RevelUser
from events import models
from events.utils import create_ticket_pdf, get_invitation_message


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
