"""Tests for email helper functions used in notification tasks."""

from unittest.mock import patch

import pytest
from django.core.exceptions import ObjectDoesNotExist

from conftest import RevelUserFactory
from events.email_helpers import build_email_context, generate_attachment_content
from events.models import Event, Organization, Ticket

pytestmark = pytest.mark.django_db


class TestBuildEmailContext:
    """Tests for build_email_context helper function."""

    def test_build_context_with_user_id(self, revel_user_factory: RevelUserFactory) -> None:
        """Test building context with a user_id."""
        # Arrange
        user = revel_user_factory()
        context_ids = {"user_id": str(user.id)}

        # Act
        context = build_email_context(context_ids)

        # Assert
        assert "user" in context
        assert context["user"] == user

    def test_build_context_with_event_id(self, event: Event) -> None:
        """Test building context with an event_id."""
        # Arrange
        context_ids = {"event_id": str(event.id)}

        # Act
        context = build_email_context(context_ids)

        # Assert
        assert "event" in context
        assert context["event"] == event
        # Verify select_related worked (no additional query needed)
        assert context["event"].organization == event.organization

    def test_build_context_with_organization_id(self, organization: Organization) -> None:
        """Test building context with an organization_id."""
        # Arrange
        context_ids = {"organization_id": str(organization.id)}

        # Act
        context = build_email_context(context_ids)

        # Assert
        assert "organization" in context
        assert context["organization"] == organization

    def test_build_context_with_ticket_id(self, event: Event, revel_user_factory: RevelUserFactory) -> None:
        """Test building context with a ticket_id."""
        # Arrange
        user = revel_user_factory()
        tier = event.ticket_tiers.first()
        assert tier is not None
        ticket = Ticket.objects.create(event=event, user=user, tier=tier, status=Ticket.Status.ACTIVE)
        context_ids = {"ticket_id": str(ticket.id)}

        # Act
        context = build_email_context(context_ids)

        # Assert
        assert "ticket" in context
        assert context["ticket"] == ticket
        # Verify select_related worked
        assert context["ticket"].event == event

    def test_build_context_with_submission_id_adds_derived_context(
        self,
        event: Event,
        revel_user_factory: RevelUserFactory,
        organization: Organization,
    ) -> None:
        """Test that submission_id adds derived context (questionnaire, submitter)."""
        # Arrange
        from questionnaires.models import Questionnaire, QuestionnaireSubmission

        user = revel_user_factory()
        questionnaire = Questionnaire.objects.create(
            name="Test Questionnaire",
        )
        submission = QuestionnaireSubmission.objects.create(
            questionnaire=questionnaire,
            user=user,
        )

        context_ids = {"submission_id": str(submission.id)}

        # Act
        context = build_email_context(context_ids)

        # Assert
        assert "submission" in context
        assert context["submission"] == submission
        # Verify derived context
        assert "questionnaire" in context
        assert context["questionnaire"] == questionnaire
        assert "submitter" in context
        assert context["submitter"] == user

    def test_build_context_passes_through_non_id_fields(self) -> None:
        """Test that non-ID fields are passed through as-is."""
        # Arrange
        context_ids = {
            "action": "created",
            "custom_field": "value",
            "number": 42,
        }

        # Act
        context = build_email_context(context_ids)

        # Assert
        assert context["action"] == "created"
        assert context["custom_field"] == "value"
        assert context["number"] == 42

    def test_build_context_with_multiple_ids(
        self,
        event: Event,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test building context with multiple model IDs."""
        # Arrange
        user = revel_user_factory()
        context_ids = {
            "user_id": str(user.id),
            "event_id": str(event.id),
            "organization_id": str(event.organization.id),
            "action": "updated",
        }

        # Act
        context = build_email_context(context_ids)

        # Assert
        assert context["user"] == user
        assert context["event"] == event
        assert context["organization"] == event.organization
        assert context["action"] == "updated"

    def test_build_context_raises_on_invalid_user_id(self) -> None:
        """Test that invalid user_id raises ObjectDoesNotExist."""
        # Arrange
        context_ids = {"user_id": "00000000-0000-0000-0000-000000000000"}

        # Act & Assert
        with pytest.raises(ObjectDoesNotExist):
            build_email_context(context_ids)

    def test_build_context_raises_on_invalid_uuid_format(self) -> None:
        """Test that invalid UUID format raises ValidationError."""
        # Arrange
        from django.core.exceptions import ValidationError

        context_ids = {"user_id": "not-a-valid-uuid"}

        # Act & Assert
        with pytest.raises(ValidationError):
            build_email_context(context_ids)


class TestGenerateAttachmentContent:
    """Tests for generate_attachment_content helper function."""

    def test_generate_ticket_pdf(self, event: Event, revel_user_factory: RevelUserFactory) -> None:
        """Test generating a ticket PDF attachment."""
        # Arrange
        user = revel_user_factory()
        tier = event.ticket_tiers.first()
        assert tier is not None
        ticket = Ticket.objects.create(event=event, user=user, tier=tier, status=Ticket.Status.ACTIVE)

        attachment_spec = {
            "type": "ticket_pdf",
            "ticket_id": str(ticket.id),
        }

        # Act
        with patch("events.email_helpers.create_ticket_pdf") as mock_create_pdf:
            mock_create_pdf.return_value = b"PDF content"
            content = generate_attachment_content(attachment_spec)

        # Assert
        assert content == b"PDF content"
        mock_create_pdf.assert_called_once()
        # Verify the ticket was fetched with select_related
        called_ticket = mock_create_pdf.call_args[0][0]
        assert called_ticket == ticket

    def test_generate_event_ics(self, event: Event) -> None:
        """Test generating an event ICS attachment."""
        # Arrange
        attachment_spec = {
            "type": "event_ics",
            "event_id": str(event.id),
        }

        # Act
        with patch.object(Event, "ics") as mock_ics:
            mock_ics.return_value = b"ICS content"
            content = generate_attachment_content(attachment_spec)

        # Assert
        assert content == b"ICS content"
        mock_ics.assert_called_once()

    def test_generate_attachment_raises_on_unknown_type(self) -> None:
        """Test that unknown attachment type raises ValueError."""
        # Arrange
        attachment_spec = {
            "type": "unknown_type",
            "some_id": "12345",
        }

        # Act & Assert
        with pytest.raises(ValueError, match="Unknown attachment type: unknown_type"):
            generate_attachment_content(attachment_spec)

    def test_generate_attachment_raises_on_missing_ticket(self) -> None:
        """Test that missing ticket raises DoesNotExist."""
        # Arrange
        attachment_spec = {
            "type": "ticket_pdf",
            "ticket_id": "00000000-0000-0000-0000-000000000000",
        }

        # Act & Assert
        with pytest.raises(Ticket.DoesNotExist):
            generate_attachment_content(attachment_spec)

    def test_generate_attachment_raises_on_missing_event(self) -> None:
        """Test that missing event raises DoesNotExist."""
        # Arrange
        attachment_spec = {
            "type": "event_ics",
            "event_id": "00000000-0000-0000-0000-000000000000",
        }

        # Act & Assert
        with pytest.raises(Event.DoesNotExist):
            generate_attachment_content(attachment_spec)

    def test_generate_attachment_raises_on_missing_type_key(self) -> None:
        """Test that missing 'type' key raises KeyError."""
        # Arrange
        attachment_spec = {"event_id": "some-id"}

        # Act & Assert
        with pytest.raises(KeyError):
            generate_attachment_content(attachment_spec)

    def test_generate_attachment_raises_on_missing_ticket_id(self) -> None:
        """Test that missing ticket_id for ticket_pdf raises KeyError."""
        # Arrange
        attachment_spec = {"type": "ticket_pdf"}

        # Act & Assert
        with pytest.raises(KeyError):
            generate_attachment_content(attachment_spec)
