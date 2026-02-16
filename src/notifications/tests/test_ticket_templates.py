"""Tests for ticket notification templates and attachment generation."""

import base64
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Organization, Ticket, TicketTier
from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.ticket_templates import (
    PaymentConfirmationTemplate,
    TicketCheckedInTemplate,
    TicketCreatedTemplate,
    TicketUpdatedTemplate,
    _build_ticket_attachments,
    _generate_ics_attachment,
    _generate_pdf_attachment,
    _generate_pkpass_attachment,
    _load_event,
    _load_ticket,
)

pytestmark = pytest.mark.django_db


# --- Fixtures ---


@pytest.fixture
def ticket_holder(django_user_model: type[RevelUser]) -> RevelUser:
    """A user who holds a ticket."""
    return django_user_model.objects.create_user(
        username="holder@example.com",
        email="holder@example.com",
        password="password",
        first_name="Ticket",
        last_name="Holder",
    )


@pytest.fixture
def ticket_organization(ticket_holder: RevelUser) -> Organization:
    """Organization for ticket tests."""
    return Organization.objects.create(
        name="Ticket Org",
        slug="ticket-org",
        owner=ticket_holder,
    )


@pytest.fixture
def ticket_event(ticket_organization: Organization) -> Event:
    """Event for ticket tests."""
    next_week = timezone.now() + timedelta(days=7)
    return Event.objects.create(
        organization=ticket_organization,
        name="Ticket Event",
        slug="ticket-event",
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        max_attendees=100,
        status="open",
        start=next_week,
        end=next_week + timedelta(hours=3),
        requires_ticket=True,
    )


@pytest.fixture
def ticket_tier(ticket_event: Event) -> TicketTier:
    """Ticket tier for tests.

    When an event is created with requires_ticket=True, a default tier is
    automatically created via signals. We return that tier instead of
    creating a new one to avoid unique constraint violations.
    """
    return ticket_event.ticket_tiers.first()  # type: ignore[return-value]


@pytest.fixture
def active_ticket(
    ticket_holder: RevelUser,
    ticket_event: Event,
    ticket_tier: TicketTier,
) -> Ticket:
    """An active ticket for testing."""
    return Ticket.objects.create(
        guest_name="Test Guest",
        user=ticket_holder,
        event=ticket_event,
        tier=ticket_tier,
        status=Ticket.TicketStatus.ACTIVE,
    )


@pytest.fixture
def pending_ticket(
    ticket_holder: RevelUser,
    ticket_event: Event,
    ticket_tier: TicketTier,
) -> Ticket:
    """A pending ticket for testing."""
    return Ticket.objects.create(
        guest_name="Test Guest",
        user=ticket_holder,
        event=ticket_event,
        tier=ticket_tier,
        status=Ticket.TicketStatus.PENDING,
    )


def _create_notification_for_test(
    user: RevelUser,
    notification_type: NotificationType,
    context: dict[str, object],
) -> Notification:
    """Create a notification directly without context validation.

    This is for unit testing templates where we only need specific context fields.
    """
    return Notification.objects.create(
        user=user,
        notification_type=notification_type,
        context=context,
    )


# --- _load_ticket Tests ---


class TestLoadTicket:
    """Tests for _load_ticket helper function."""

    def test_loads_ticket_with_related_objects(self, active_ticket: Ticket) -> None:
        """Should load ticket with event, user, and tier prefetched."""
        result = _load_ticket(str(active_ticket.id))

        assert result is not None
        assert result.id == active_ticket.id
        # Verify related objects are loaded
        assert result.event.name == active_ticket.event.name
        assert result.user.email == active_ticket.user.email
        assert result.tier.name == active_ticket.tier.name

    def test_returns_none_for_nonexistent_ticket(self) -> None:
        """Should return None and log warning for nonexistent ticket."""
        fake_id = str(uuid.uuid4())

        result = _load_ticket(fake_id)

        assert result is None


# --- _load_event Tests ---


class TestLoadEvent:
    """Tests for _load_event helper function."""

    def test_loads_event_with_city_prefetched(self, ticket_event: Event) -> None:
        """Should load event with city prefetched."""
        result = _load_event(str(ticket_event.id))

        assert result is not None
        assert result.id == ticket_event.id

    def test_returns_none_for_nonexistent_event(self) -> None:
        """Should return None and log warning for nonexistent event."""
        fake_id = str(uuid.uuid4())

        result = _load_event(fake_id)

        assert result is None


# --- _generate_pdf_attachment Tests ---


class TestGeneratePdfAttachment:
    """Tests for _generate_pdf_attachment helper function."""

    @patch("events.utils.create_ticket_pdf")
    def test_generates_pdf_with_base64_encoding(
        self,
        mock_create_pdf: MagicMock,
        active_ticket: Ticket,
    ) -> None:
        """Should return (attachment_dict, raw_bytes) tuple."""
        pdf_bytes = b"%PDF-1.4 test content"
        mock_create_pdf.return_value = pdf_bytes

        result = _generate_pdf_attachment(active_ticket)

        assert result is not None
        attachment, raw_bytes = result
        assert attachment["mimetype"] == "application/pdf"
        assert attachment["content_base64"] == base64.b64encode(pdf_bytes).decode("utf-8")
        assert raw_bytes == pdf_bytes
        mock_create_pdf.assert_called_once_with(active_ticket)

    @patch("events.utils.create_ticket_pdf")
    def test_returns_none_on_exception(
        self,
        mock_create_pdf: MagicMock,
        active_ticket: Ticket,
    ) -> None:
        """Should return None and log exception on failure."""
        mock_create_pdf.side_effect = Exception("PDF generation failed")

        result = _generate_pdf_attachment(active_ticket)

        assert result is None


# --- _generate_ics_attachment Tests ---


class TestGenerateIcsAttachment:
    """Tests for _generate_ics_attachment helper function."""

    @patch.object(Event, "ics")
    def test_generates_ics_with_base64_encoding(
        self,
        mock_ics: MagicMock,
        ticket_event: Event,
    ) -> None:
        """Should return base64-encoded ICS content."""
        ics_bytes = b"BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR"
        mock_ics.return_value = ics_bytes

        result = _generate_ics_attachment(ticket_event)

        assert result is not None
        assert result["mimetype"] == "text/calendar"
        assert result["content_base64"] == base64.b64encode(ics_bytes).decode("utf-8")

    @patch.object(Event, "ics")
    def test_returns_none_on_exception(
        self,
        mock_ics: MagicMock,
        ticket_event: Event,
    ) -> None:
        """Should return None and log exception on failure."""
        mock_ics.side_effect = Exception("ICS generation failed")

        result = _generate_ics_attachment(ticket_event)

        assert result is None


# --- _generate_pkpass_attachment Tests ---


class TestGeneratePkpassAttachment:
    """Tests for _generate_pkpass_attachment helper function."""

    def test_returns_none_when_apple_pass_not_available(
        self,
        active_ticket: Ticket,
    ) -> None:
        """Should return None when Apple Wallet is not configured."""
        # Mock apple_pass_available to return False
        with patch.object(Ticket, "apple_pass_available", False):
            result = _generate_pkpass_attachment(active_ticket)

        assert result is None

    @patch("events.service.ticket_file_service.get_apple_pass_generator")
    def test_generates_pkpass_with_base64_encoding(
        self,
        mock_get_generator: MagicMock,
        active_ticket: Ticket,
    ) -> None:
        """Should return (attachment_dict, raw_bytes) tuple when available."""
        pkpass_bytes = b"PK\x03\x04 pkpass content"
        mock_generator = MagicMock()
        mock_generator.generate_pass.return_value = pkpass_bytes
        mock_get_generator.return_value = mock_generator

        # Mock apple_pass_available to return True
        with patch.object(Ticket, "apple_pass_available", True):
            result = _generate_pkpass_attachment(active_ticket)

        assert result is not None
        attachment, raw_bytes = result
        assert attachment["mimetype"] == "application/vnd.apple.pkpass"
        assert attachment["content_base64"] == base64.b64encode(pkpass_bytes).decode("utf-8")
        assert raw_bytes == pkpass_bytes
        mock_generator.generate_pass.assert_called_once_with(active_ticket)

    @patch("events.service.ticket_file_service.get_apple_pass_generator")
    def test_returns_none_on_exception(
        self,
        mock_get_generator: MagicMock,
        active_ticket: Ticket,
    ) -> None:
        """Should return None and log exception on failure."""
        mock_generator = MagicMock()
        mock_generator.generate_pass.side_effect = Exception("pkpass generation failed")
        mock_get_generator.return_value = mock_generator

        with patch.object(Ticket, "apple_pass_available", True):
            result = _generate_pkpass_attachment(active_ticket)

        assert result is None


# --- _build_ticket_attachments Tests ---


class TestBuildTicketAttachments:
    """Tests for _build_ticket_attachments helper function."""

    def test_returns_empty_dict_when_no_ticket_id(
        self,
        ticket_event: Event,
    ) -> None:
        """Should return empty dict when ticket_id is None."""
        result = _build_ticket_attachments(
            ticket_id=None,
            event_id=str(ticket_event.id),
        )

        assert result == {}

    def test_returns_empty_dict_when_no_event_id(
        self,
        active_ticket: Ticket,
    ) -> None:
        """Should return empty dict when event_id is None."""
        result = _build_ticket_attachments(
            ticket_id=str(active_ticket.id),
            event_id=None,
        )

        assert result == {}

    @patch("notifications.service.templates.ticket_templates._generate_pdf_attachment")
    @patch("notifications.service.templates.ticket_templates._generate_ics_attachment")
    @patch("notifications.service.templates.ticket_templates._generate_pkpass_attachment")
    def test_includes_pdf_when_include_pdf_true(
        self,
        mock_pkpass: MagicMock,
        mock_ics: MagicMock,
        mock_pdf: MagicMock,
        active_ticket: Ticket,
    ) -> None:
        """Should include PDF attachment when include_pdf is True."""
        pdf_att = {"content_base64": "pdf_content", "mimetype": "application/pdf"}
        mock_pdf.return_value = (pdf_att, b"pdf_raw")
        mock_ics.return_value = None
        mock_pkpass.return_value = None

        result = _build_ticket_attachments(
            ticket_id=str(active_ticket.id),
            event_id=str(active_ticket.event.id),
            include_pdf=True,
            include_ics=False,
            include_pkpass=False,
        )

        assert "ticket.pdf" in result
        assert result["ticket.pdf"]["mimetype"] == "application/pdf"

    @patch("notifications.service.templates.ticket_templates._generate_pdf_attachment")
    @patch("notifications.service.templates.ticket_templates._generate_ics_attachment")
    @patch("notifications.service.templates.ticket_templates._generate_pkpass_attachment")
    def test_includes_ics_when_include_ics_true(
        self,
        mock_pkpass: MagicMock,
        mock_ics: MagicMock,
        mock_pdf: MagicMock,
        active_ticket: Ticket,
    ) -> None:
        """Should include ICS attachment when include_ics is True."""
        mock_pdf.return_value = None
        mock_ics.return_value = {"content_base64": "ics_content", "mimetype": "text/calendar"}
        mock_pkpass.return_value = None

        result = _build_ticket_attachments(
            ticket_id=str(active_ticket.id),
            event_id=str(active_ticket.event.id),
            include_pdf=False,
            include_ics=True,
            include_pkpass=False,
        )

        assert "event.ics" in result
        assert result["event.ics"]["mimetype"] == "text/calendar"

    @patch("notifications.service.templates.ticket_templates._generate_pdf_attachment")
    @patch("notifications.service.templates.ticket_templates._generate_ics_attachment")
    @patch("notifications.service.templates.ticket_templates._generate_pkpass_attachment")
    def test_includes_pkpass_when_include_pkpass_true(
        self,
        mock_pkpass: MagicMock,
        mock_ics: MagicMock,
        mock_pdf: MagicMock,
        active_ticket: Ticket,
    ) -> None:
        """Should include pkpass attachment when include_pkpass is True."""
        mock_pdf.return_value = None
        mock_ics.return_value = None
        pkpass_att = {
            "content_base64": "pkpass_content",
            "mimetype": "application/vnd.apple.pkpass",
        }
        mock_pkpass.return_value = (pkpass_att, b"pkpass_raw")

        result = _build_ticket_attachments(
            ticket_id=str(active_ticket.id),
            event_id=str(active_ticket.event.id),
            include_pdf=False,
            include_ics=False,
            include_pkpass=True,
        )

        assert "ticket.pkpass" in result
        assert result["ticket.pkpass"]["mimetype"] == "application/vnd.apple.pkpass"

    @patch("notifications.service.templates.ticket_templates._generate_pdf_attachment")
    @patch("notifications.service.templates.ticket_templates._generate_ics_attachment")
    @patch("notifications.service.templates.ticket_templates._generate_pkpass_attachment")
    def test_includes_all_attachments(
        self,
        mock_pkpass: MagicMock,
        mock_ics: MagicMock,
        mock_pdf: MagicMock,
        active_ticket: Ticket,
    ) -> None:
        """Should include all attachments when all flags are True."""
        mock_pdf.return_value = (
            {"content_base64": "pdf", "mimetype": "application/pdf"},
            b"pdf_raw",
        )
        mock_ics.return_value = {"content_base64": "ics", "mimetype": "text/calendar"}
        mock_pkpass.return_value = (
            {"content_base64": "pkpass", "mimetype": "application/vnd.apple.pkpass"},
            b"pkpass_raw",
        )

        result = _build_ticket_attachments(
            ticket_id=str(active_ticket.id),
            event_id=str(active_ticket.event.id),
            include_pdf=True,
            include_ics=True,
            include_pkpass=True,
        )

        assert len(result) == 3
        assert "ticket.pdf" in result
        assert "event.ics" in result
        assert "ticket.pkpass" in result

    @patch("notifications.service.templates.ticket_templates._generate_pdf_attachment")
    @patch("notifications.service.templates.ticket_templates._generate_ics_attachment")
    @patch("notifications.service.templates.ticket_templates._generate_pkpass_attachment")
    def test_excludes_none_attachments(
        self,
        mock_pkpass: MagicMock,
        mock_ics: MagicMock,
        mock_pdf: MagicMock,
        active_ticket: Ticket,
    ) -> None:
        """Should not include attachments that return None."""
        mock_pdf.return_value = (
            {"content_base64": "pdf", "mimetype": "application/pdf"},
            b"pdf_raw",
        )
        mock_ics.return_value = None  # Failed to generate
        mock_pkpass.return_value = None  # Not configured

        result = _build_ticket_attachments(
            ticket_id=str(active_ticket.id),
            event_id=str(active_ticket.event.id),
            include_pdf=True,
            include_ics=True,
            include_pkpass=True,
        )

        assert len(result) == 1
        assert "ticket.pdf" in result
        assert "event.ics" not in result
        assert "ticket.pkpass" not in result

    @patch("events.service.ticket_file_service.cache_files")
    @patch("notifications.service.templates.ticket_templates._generate_pdf_attachment")
    @patch("notifications.service.templates.ticket_templates._generate_ics_attachment")
    @patch("notifications.service.templates.ticket_templates._generate_pkpass_attachment")
    def test_caches_generated_files_via_service(
        self,
        mock_pkpass: MagicMock,
        mock_ics: MagicMock,
        mock_pdf: MagicMock,
        mock_cache_files: MagicMock,
        active_ticket: Ticket,
    ) -> None:
        """Should call ticket_file_service.cache_files with raw bytes."""
        mock_pdf.return_value = (
            {"content_base64": "pdf_b64", "mimetype": "application/pdf"},
            b"pdf_raw_bytes",
        )
        mock_ics.return_value = None
        pkpass_att = {"content_base64": "pk_b64", "mimetype": "application/vnd.apple.pkpass"}
        mock_pkpass.return_value = (pkpass_att, b"pkpass_raw_bytes")

        _build_ticket_attachments(
            ticket_id=str(active_ticket.id),
            event_id=str(active_ticket.event.id),
            include_pdf=True,
            include_ics=False,
            include_pkpass=True,
        )

        mock_cache_files.assert_called_once()
        call_kwargs = mock_cache_files.call_args[1]
        assert call_kwargs["pdf_bytes"] == b"pdf_raw_bytes"
        assert call_kwargs["pkpass_bytes"] == b"pkpass_raw_bytes"

    @patch("events.service.ticket_file_service.cache_files")
    @patch("notifications.service.templates.ticket_templates._generate_pdf_attachment")
    @patch("notifications.service.templates.ticket_templates._generate_ics_attachment")
    @patch("notifications.service.templates.ticket_templates._generate_pkpass_attachment")
    def test_does_not_cache_when_no_files_generated(
        self,
        mock_pkpass: MagicMock,
        mock_ics: MagicMock,
        mock_pdf: MagicMock,
        mock_cache_files: MagicMock,
        active_ticket: Ticket,
    ) -> None:
        """Should not call cache_files when pdf and pkpass both fail."""
        mock_pdf.return_value = None
        mock_ics.return_value = {"content_base64": "ics", "mimetype": "text/calendar"}
        mock_pkpass.return_value = None

        _build_ticket_attachments(
            ticket_id=str(active_ticket.id),
            event_id=str(active_ticket.event.id),
            include_pdf=True,
            include_ics=True,
            include_pkpass=True,
        )

        mock_cache_files.assert_not_called()


# --- Template Class Tests ---


class TestTicketCreatedTemplate:
    """Tests for TicketCreatedTemplate."""

    def test_get_in_app_title_for_ticket_holder(
        self,
        ticket_holder: RevelUser,
    ) -> None:
        """Should return appropriate title for ticket holder."""
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.TICKET_CREATED,
            context={
                "event_name": "Ticket Event",
                "ticket_status": "active",
            },
        )
        template = TicketCreatedTemplate()

        title = template.get_in_app_title(notification)

        assert "Ticket Event" in title
        assert "Confirmed" in title

    def test_get_in_app_title_for_pending_ticket(
        self,
        ticket_holder: RevelUser,
    ) -> None:
        """Should return pending title for pending ticket."""
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.TICKET_CREATED,
            context={
                "event_name": "Ticket Event",
                "ticket_status": "pending",
            },
        )
        template = TicketCreatedTemplate()

        title = template.get_in_app_title(notification)

        assert "Pending" in title

    def test_get_in_app_title_for_staff_notification(
        self,
        ticket_holder: RevelUser,
    ) -> None:
        """Should return holder name for staff notifications."""
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.TICKET_CREATED,
            context={
                "event_name": "Ticket Event",
                "ticket_holder_name": "John Doe",
            },
        )
        template = TicketCreatedTemplate()

        title = template.get_in_app_title(notification)

        assert "John Doe" in title
        assert "New Ticket" in title

    @patch("notifications.service.templates.ticket_templates._build_ticket_attachments")
    def test_get_email_attachments_calls_build_function(
        self,
        mock_build: MagicMock,
        ticket_holder: RevelUser,
        active_ticket: Ticket,
    ) -> None:
        """Should call _build_ticket_attachments with correct params."""
        mock_build.return_value = {}
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.TICKET_CREATED,
            context={
                "ticket_id": str(active_ticket.id),
                "event_id": str(active_ticket.event.id),
                "event_name": "Ticket Event",
                "include_pdf": True,
                "include_ics": True,
                "include_pkpass": True,
            },
        )
        template = TicketCreatedTemplate()

        template.get_email_attachments(notification)

        mock_build.assert_called_once()
        call_kwargs = mock_build.call_args[1]
        assert call_kwargs["include_pdf"] is True
        assert call_kwargs["include_ics"] is True
        assert call_kwargs["include_pkpass"] is True


class TestTicketUpdatedTemplate:
    """Tests for TicketUpdatedTemplate."""

    def test_get_in_app_title_for_activation(
        self,
        ticket_holder: RevelUser,
    ) -> None:
        """Should return confirmed title when ticket is activated."""
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.TICKET_UPDATED,
            context={
                "event_name": "Ticket Event",
                "old_status": "pending",
                "new_status": "active",
            },
        )
        template = TicketUpdatedTemplate()

        title = template.get_in_app_title(notification)

        assert "Confirmed" in title

    def test_get_in_app_title_for_general_update(
        self,
        ticket_holder: RevelUser,
    ) -> None:
        """Should return update title for general updates."""
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.TICKET_UPDATED,
            context={
                "event_name": "Ticket Event",
                "action": "updated",
            },
        )
        template = TicketUpdatedTemplate()

        title = template.get_in_app_title(notification)

        assert "Update" in title

    @patch("notifications.service.templates.ticket_templates._build_ticket_attachments")
    def test_get_email_attachments_no_attachments_for_cancellation(
        self,
        mock_build: MagicMock,
        ticket_holder: RevelUser,
    ) -> None:
        """Should return empty dict for cancellations."""
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.TICKET_UPDATED,
            context={
                "event_name": "Ticket Event",
                "include_pdf": False,
                "include_ics": False,
                "include_pkpass": False,
            },
        )
        template = TicketUpdatedTemplate()

        result = template.get_email_attachments(notification)

        assert result == {}
        mock_build.assert_not_called()


class TestPaymentConfirmationTemplate:
    """Tests for PaymentConfirmationTemplate."""

    def test_get_in_app_title(
        self,
        ticket_holder: RevelUser,
    ) -> None:
        """Should return payment confirmation title."""
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.PAYMENT_CONFIRMATION,
            context={"event_name": "Test Event"},  # Minimal context
        )
        template = PaymentConfirmationTemplate()

        title = template.get_in_app_title(notification)

        assert "Payment Confirmation" in title

    @patch("notifications.service.templates.ticket_templates._build_ticket_attachments")
    def test_get_email_attachments_includes_all(
        self,
        mock_build: MagicMock,
        ticket_holder: RevelUser,
        active_ticket: Ticket,
    ) -> None:
        """Should include all attachments for payment confirmation."""
        mock_build.return_value = {}
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.PAYMENT_CONFIRMATION,
            context={
                "ticket_id": str(active_ticket.id),
                "event_id": str(active_ticket.event.id),
                "event_name": active_ticket.event.name,
            },
        )
        template = PaymentConfirmationTemplate()

        template.get_email_attachments(notification)

        mock_build.assert_called_once()
        call_kwargs = mock_build.call_args[1]
        assert call_kwargs["include_pdf"] is True
        assert call_kwargs["include_ics"] is True
        assert call_kwargs["include_pkpass"] is True


class TestTicketCheckedInTemplate:
    """Tests for TicketCheckedInTemplate."""

    def test_get_in_app_title(
        self,
        ticket_holder: RevelUser,
        active_ticket: Ticket,
    ) -> None:
        """Should return checked in title with event name."""
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.TICKET_CHECKED_IN,
            context={
                "event_name": active_ticket.event.name,
            },
        )
        template = TicketCheckedInTemplate()

        title = template.get_in_app_title(notification)

        assert "Checked in" in title
        assert active_ticket.event.name in title

    def test_get_email_subject(
        self,
        ticket_holder: RevelUser,
        active_ticket: Ticket,
    ) -> None:
        """Should return checked in subject with event name."""
        notification = _create_notification_for_test(
            user=ticket_holder,
            notification_type=NotificationType.TICKET_CHECKED_IN,
            context={
                "event_name": active_ticket.event.name,
            },
        )
        template = TicketCheckedInTemplate()

        subject = template.get_email_subject(notification)

        assert "Checked in" in subject
        assert active_ticket.event.name in subject
